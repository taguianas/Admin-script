#!/usr/bin/env python3
"""
restore.py — Restore files from a Windows incremental backup snapshot

Lists available snapshots, lets you pick one interactively or specify it
via CLI, then uses robocopy to restore the files.

Usage:
    python restore.py
    python restore.py --snapshot 2026-03-01_020000
    python restore.py --snapshot latest --target D:\\Restore --verify
    python restore.py --snapshot 2026-03-01_020000 --path C\\Users\\alice --dry-run
"""

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.logger import get_logger
from common.config_loader import load_config

# Import helpers from the backup module (same package)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_incremental import (
    LATEST_FILE,
    MANIFEST_FILE,
    REPORT_FILE,
    TIMESTAMP_FMT,
    list_snapshots,
    get_latest_snapshot,
)

logger = get_logger(__name__, log_dir=_PROJECT_ROOT / "logs")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _snapshot_size(snap: Path) -> str:
    total = sum(
        f.stat().st_size
        for f in snap.rglob("*")
        if f.is_file() and f.name not in (MANIFEST_FILE, REPORT_FILE)
    )
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024:
            return f"{total:.0f} {unit}"
        total //= 1024
    return f"{total} PB"


def print_snapshot_table(dest: Path) -> list[Path]:
    snaps = list_snapshots(dest)
    if not snaps:
        print(f"No snapshots found in: {dest}")
        return []

    latest_name = (dest / LATEST_FILE).read_text().strip() if (dest / LATEST_FILE).exists() else ""

    print()
    print(f"  {'#':<4}  {'Snapshot':<24}  {'Size':<10}  {'Report'}")
    print(f"  {'----':<4}  {'------------------------':<24}  {'----------':<10}  {'------'}")
    for i, snap in enumerate(snaps, 1):
        tag    = " (latest)" if snap.name == latest_name else ""
        size   = _snapshot_size(snap)
        report = "yes" if (snap / REPORT_FILE).exists() else "-"
        print(f"  {i:<4}  {snap.name + tag:<24}  {size:<10}  {report}")
    print()
    return snaps


def show_report(snap: Path) -> None:
    report_file = snap / REPORT_FILE
    if report_file.exists():
        print()
        print("  --- Snapshot Report ---")
        for line in report_file.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")
        print()


# ---------------------------------------------------------------------------
# Snapshot selection
# ---------------------------------------------------------------------------

def select_snapshot_interactive(dest: Path) -> Path:
    snaps = print_snapshot_table(dest)
    if not snaps:
        sys.exit(1)

    while True:
        choice = input("  Enter snapshot number (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            print("  Restore cancelled.")
            sys.exit(0)
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(snaps):
                return snaps[idx]
        print(f"  Please enter a number between 1 and {len(snaps)}.")


def resolve_snapshot(dest: Path, name: str | None) -> Path:
    if name is None:
        return select_snapshot_interactive(dest)
    if name == "latest":
        snap = get_latest_snapshot(dest)
        if snap is None:
            logger.error("No 'latest' snapshot found in: %s", dest)
            sys.exit(1)
        return snap
    candidate = dest / name
    if not candidate.is_dir():
        logger.error("Snapshot not found: %s", candidate)
        sys.exit(1)
    return candidate


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------

def verify_checksums(snap: Path) -> bool:
    manifest_path = snap / MANIFEST_FILE
    if not manifest_path.exists():
        logger.warning("No manifest found at %s — skipping verification", manifest_path)
        return True

    logger.info("Verifying checksums from: %s", manifest_path)
    failed = 0
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        expected, rel_str = parts
        filepath = snap / rel_str.strip()
        if not filepath.exists():
            logger.warning("Missing file: %s", filepath)
            continue
        chunk = 1 << 20
        h = hashlib.sha256()
        with filepath.open("rb") as fh:
            while data := fh.read(chunk):
                h.update(data)
        if h.hexdigest() != expected:
            logger.error("Checksum mismatch: %s", filepath)
            failed += 1

    if failed:
        logger.error("Verification failed: %d file(s) corrupted", failed)
        return False
    logger.info("All checksums OK")
    return True


# ---------------------------------------------------------------------------
# Restore via robocopy
# ---------------------------------------------------------------------------

def do_restore(
    snap: Path,
    restore_path: str | None,
    target: Path | None,
    dry_run: bool,
) -> None:
    # Source: the full snapshot dir, or a subdirectory within it
    if restore_path:
        source = snap / restore_path.lstrip("/\\")
        if not source.exists():
            logger.error("Path not found in snapshot: %s", source)
            sys.exit(1)
    else:
        source = snap

    # Destination
    if target is not None:
        dest = target
        dest.mkdir(parents=True, exist_ok=True)
    else:
        # In-place: restore to the root of the drive (C:\ or D:\)
        # The snapshot stores files as <drive_label>\<rest>, e.g. C\Users\alice
        dest = Path(os.environ.get("SystemDrive", "C:") + "\\")

    logger.info("Restoring from : %s", source)
    logger.info("Restoring to   : %s (%s)", dest, "staging" if target else "in-place")

    cmd = [
        "robocopy",
        str(source), str(dest),
        "/E",           # recurse all subdirectories
        "/COPYALL",     # copy all file attributes
        "/R:3",
        "/W:5",
    ]
    if dry_run:
        cmd.append("/L")

    logger.info("robocopy: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode >= 8:
        logger.error("robocopy failed (exit %d):\n%s", result.returncode, result.stderr)
        sys.exit(1)

    logger.info("Restore completed (robocopy exit code: %d)", result.returncode)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Restore files from a Windows incremental backup snapshot."
    )
    p.add_argument("--config", default=None,
                   help="Path to backup_config.yaml")
    p.add_argument("--dest", default=None,
                   help="Backup destination root (overrides config)")
    p.add_argument("--snapshot", default=None,
                   help='Snapshot name (e.g. 2026-03-01_020000) or "latest"')
    p.add_argument("--path", default=None,
                   help="Relative path within snapshot to restore (e.g. C\\\\Users\\\\alice)")
    p.add_argument("--target", default=None,
                   help="Where to restore files (default: original in-place paths)")
    p.add_argument("--verify", action="store_true",
                   help="Verify SHA-256 checksums before restoring")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be restored without doing it")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    default_cfg = _PROJECT_ROOT / "backup" / "config" / "backup_config.yaml"
    cfg_path    = Path(args.config) if args.config else default_cfg
    cfg         = load_config(cfg_path) if cfg_path.exists() else {}

    destination = args.dest or cfg.get("backup", {}).get("destination", "")
    if not destination:
        logger.error("No backup destination configured. Use --dest or backup_config.yaml.")
        sys.exit(1)

    dest_path = Path(destination)
    if not dest_path.exists():
        logger.error("Backup destination not found: %s", dest_path)
        sys.exit(1)

    logger.info("=== restore.py started ===")

    snap = resolve_snapshot(dest_path, args.snapshot)
    logger.info("Selected snapshot: %s", snap)
    show_report(snap)

    # Confirm in-place restore
    target = Path(args.target) if args.target else None
    if target is None and not args.dry_run:
        print()
        print("  WARNING: IN-PLACE RESTORE — files will overwrite their original paths.")
        confirm = input("  Type 'yes' to continue: ").strip()
        if confirm.lower() != "yes":
            print("  Restore cancelled.")
            sys.exit(0)

    if args.verify:
        if not verify_checksums(snap):
            logger.error("Aborting restore due to checksum failures.")
            sys.exit(1)

    do_restore(snap, args.path, target, args.dry_run)
    logger.info("=== restore.py finished ===")


if __name__ == "__main__":
    main()
