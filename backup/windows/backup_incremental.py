#!/usr/bin/env python3
"""
backup_incremental.py — Hardlink-based incremental backup for Windows (NTFS)

How it works:
    Each run creates a new timestamped snapshot directory.  For every file in
    the source tree:
      - If the file is identical to the latest snapshot (same size + mtime):
        create a hardlink pointing to the previous copy  →  zero extra disk space
      - If the file is new or changed: copy it fresh

    This mirrors rsync --link-dest behaviour on Linux and produces space-efficient
    full-snapshot directories on any NTFS volume.

    Falls back to robocopy for UNC network paths (\\\\server\\share) where
    hardlinks are not supported.

Snapshot layout:
    D:\\Backups\\
    ├── 2026-03-01_020000\\
    │   ├── Users\\
    │   ├── inetpub\\
    │   ├── .manifest.sha256
    │   └── .report.txt
    ├── 2026-03-02_020000\\
    └── latest.txt          (text file containing the name of the latest snapshot)

Usage:
    # Normal run (reads config from backup\\config\\backup_config.yaml)
    python backup_incremental.py

    # Override config
    python backup_incremental.py --config D:\\backup_config.yaml

    # Override specific settings
    python backup_incremental.py --source C:\\Users --dest D:\\Backups --retention 14

    # Dry run
    python backup_incremental.py --dry-run
"""

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

# Resolve project root so common imports work
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.logger import get_logger
from common.config_loader import load_config, get_nested
from common.notifier import Notifier

logger = get_logger(__name__, log_dir=_PROJECT_ROOT / "logs")

TIMESTAMP_FMT = "%Y-%m-%d_%H%M%S"
LATEST_FILE   = "latest.txt"     # stores name of latest snapshot (no symlinks on FAT)
MANIFEST_FILE = ".manifest.sha256"
REPORT_FILE   = ".report.txt"
LOCK_FILE     = Path(os.environ.get("TEMP", "C:\\Windows\\Temp")) / "backup_incremental.lock"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class BackupStats(NamedTuple):
    files_copied:   int
    files_linked:   int
    files_skipped:  int
    bytes_copied:   int
    duration_secs:  float
    errors:         list[str]


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------

class LockFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self):
        if self._path.exists():
            pid = self._path.read_text().strip()
            # Check if the PID is still alive (best-effort)
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True
                )
                if pid in result.stdout:
                    raise RuntimeError(
                        f"Backup already running (PID {pid}). "
                        f"Remove {self._path} if this is incorrect."
                    )
            except (subprocess.SubprocessError, ValueError):
                pass
            logger.warning("Stale lock file found — removing it")
        self._path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *_):
        self._path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def list_snapshots(dest: Path) -> list[Path]:
    """Return all timestamped snapshot directories, sorted oldest-first."""
    return sorted(
        [d for d in dest.iterdir() if d.is_dir() and d.name[:4].isdigit()],
        key=lambda d: d.name,
    )


def get_latest_snapshot(dest: Path) -> Path | None:
    """Return the path of the most recent snapshot, or None."""
    latest_file = dest / LATEST_FILE
    if latest_file.exists():
        name = latest_file.read_text().strip()
        candidate = dest / name
        if candidate.is_dir():
            return candidate
    # Fall back to most recently named directory
    snapshots = list_snapshots(dest)
    return snapshots[-1] if snapshots else None


def update_latest_pointer(dest: Path, snapshot_name: str) -> None:
    (dest / LATEST_FILE).write_text(snapshot_name)


def rotate_old_snapshots(dest: Path, retention_days: int, dry_run: bool) -> int:
    """Delete snapshots older than retention_days. Returns count removed."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for snap in list_snapshots(dest):
        try:
            snap_dt = datetime.strptime(snap.name, TIMESTAMP_FMT)
        except ValueError:
            continue
        if snap_dt < cutoff:
            if dry_run:
                logger.info("[DRY-RUN] Would remove: %s", snap)
            else:
                shutil.rmtree(snap)
                logger.info("Removed old snapshot: %s", snap)
            removed += 1
    if removed == 0:
        logger.info("No snapshots to rotate")
    return removed


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while data := fh.read(chunk):
            h.update(data)
    return h.hexdigest()


def files_are_identical(src: Path, ref: Path) -> bool:
    """Quick equality check: same size and modification time."""
    try:
        ss = src.stat()
        rs = ref.stat()
        return (ss.st_size == rs.st_size and
                abs(ss.st_mtime - rs.st_mtime) < 2.0)  # 2-second tolerance
    except OSError:
        return False


def is_unc_path(path: Path) -> bool:
    return str(path).startswith("\\\\")


def matches_exclude(rel_path: str, patterns: list[str]) -> bool:
    """Return True if any component of rel_path matches any exclude pattern."""
    import fnmatch
    parts = Path(rel_path).parts   # all path components including filename
    for pat in patterns:
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


# ---------------------------------------------------------------------------
# Hardlink-based copy (primary method for local NTFS volumes)
# ---------------------------------------------------------------------------

def backup_with_hardlinks(
    source: Path,
    snapshot_dir: Path,
    prev_snapshot_source: Path | None,
    excludes: list[str],
    dry_run: bool,
) -> BackupStats:
    """
    Walk source, hardlink unchanged files from prev_snapshot_source, copy new/changed.
    Returns BackupStats.
    """
    files_copied  = 0
    files_linked  = 0
    files_skipped = 0
    bytes_copied  = 0
    errors: list[str] = []

    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        rel_root  = root_path.relative_to(source)

        # Filter excluded directories in-place so os.walk skips them
        dirs[:] = [
            d for d in dirs
            if not matches_exclude(str(rel_root / d), excludes)
        ]

        for filename in files:
            src_file = root_path / filename
            rel_file = rel_root / filename

            if matches_exclude(str(rel_file), excludes):
                files_skipped += 1
                continue

            # Compute destination path — store under snapshot/<drive_letter>/<rest>
            # e.g. C:\Users\alice → snapshot\C\Users\alice
            drive, tail = os.path.splitdrive(str(source))
            drive_label = drive.rstrip(":") or "root"
            dst_rel  = Path(drive_label) / rel_file
            dst_file = snapshot_dir / dst_rel

            if dry_run:
                logger.debug("[DRY-RUN] Would back up: %s", src_file)
                files_copied += 1
                continue

            dst_file.parent.mkdir(parents=True, exist_ok=True)

            # Try hardlink from previous snapshot first
            if prev_snapshot_source is not None:
                # Files are stored as <snapshot_root>/<drive_label>/<relative_path>
                prev_file = prev_snapshot_source / drive_label / rel_file

                if prev_file.exists() and files_are_identical(src_file, prev_file):
                    try:
                        os.link(prev_file, dst_file)
                        files_linked += 1
                        continue
                    except OSError:
                        pass  # hardlink failed (cross-device, permission, etc.) — fall through to copy

            # Copy the file
            try:
                shutil.copy2(src_file, dst_file)
                files_copied += 1
                bytes_copied += src_file.stat().st_size
            except OSError as exc:
                logger.warning("Could not copy %s: %s", src_file, exc)
                errors.append(f"{src_file}: {exc}")

    return BackupStats(
        files_copied=files_copied,
        files_linked=files_linked,
        files_skipped=files_skipped,
        bytes_copied=bytes_copied,
        duration_secs=0.0,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# robocopy fallback (UNC paths / network shares)
# ---------------------------------------------------------------------------

def backup_with_robocopy(
    source: Path,
    dest_dir: Path,
    excludes: list[str],
    dry_run: bool,
    log_file: Path,
) -> BackupStats:
    """Run robocopy for UNC or network paths; return parsed stats."""
    exclude_dirs  = [e for e in excludes if "*" not in e]
    exclude_files = [e for e in excludes if "*" in e]

    cmd = [
        "robocopy",
        str(source), str(dest_dir),
        "/E",           # all subdirs including empty
        "/DCOPY:DAT",   # copy dir timestamps
        "/COPY:DAT",    # copy data, attributes, timestamps
        "/R:3",         # 3 retries
        "/W:5",         # 5 second wait between retries
        f"/LOG+:{log_file}",
    ]
    if dry_run:
        cmd.append("/L")   # list only
    if exclude_dirs:
        cmd += ["/XD"] + exclude_dirs
    if exclude_files:
        cmd += ["/XF"] + exclude_files

    logger.info("robocopy: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)
    # robocopy exit codes: 0=no change, 1=copied, 2=extra, 4=mismatched,
    # 8=failed, 16=fatal. <8 = success.
    if result.returncode >= 8:
        raise RuntimeError(
            f"robocopy failed (exit {result.returncode}): {result.stderr}"
        )

    # Parse simple stats from output
    copied = 0
    for line in result.stdout.splitlines():
        if "Files :" in line:
            parts = line.split()
            try:
                copied = int(parts[2])
            except (IndexError, ValueError):
                pass

    return BackupStats(
        files_copied=copied,
        files_linked=0,
        files_skipped=0,
        bytes_copied=0,
        duration_secs=0.0,
        errors=[],
    )


# ---------------------------------------------------------------------------
# SHA-256 manifest
# ---------------------------------------------------------------------------

def generate_manifest(snapshot_dir: Path) -> int:
    """Write .manifest.sha256 for all files in snapshot_dir. Returns file count."""
    manifest_path = snapshot_dir / MANIFEST_FILE
    count = 0
    with manifest_path.open("w", encoding="utf-8") as mf:
        for root, _, files in os.walk(snapshot_dir):
            for name in files:
                if name in (MANIFEST_FILE, REPORT_FILE):
                    continue
                filepath = Path(root) / name
                try:
                    digest = file_sha256(filepath)
                    rel    = filepath.relative_to(snapshot_dir)
                    mf.write(f"{digest}  {rel}\n")
                    count += 1
                except OSError as exc:
                    logger.warning("Could not hash %s: %s", filepath, exc)
    logger.info("Manifest written: %s (%d files)", manifest_path, count)
    return count


# ---------------------------------------------------------------------------
# Backup report
# ---------------------------------------------------------------------------

def write_report(
    snapshot_dir: Path,
    sources: list[str],
    stats: BackupStats,
    retention_days: int,
    dry_run: bool,
) -> None:
    total_size = sum(
        f.stat().st_size
        for f in snapshot_dir.rglob("*")
        if f.is_file() and f.name not in (MANIFEST_FILE, REPORT_FILE)
    )

    def fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n //= 1024
        return f"{n} PB"

    dur = stats.duration_secs
    dur_str = f"{int(dur // 3600):02d}:{int((dur % 3600) // 60):02d}:{int(dur % 60):02d}"

    report = snapshot_dir / REPORT_FILE
    report.write_text(
        f"Backup Report\n"
        f"=============\n"
        f"Timestamp      : {snapshot_dir.name}\n"
        f"Snapshot       : {snapshot_dir}\n"
        f"Duration       : {dur_str}\n"
        f"Total size     : {fmt_bytes(total_size)}\n"
        f"Sources        : {', '.join(sources)}\n\n"
        f"File counts\n"
        f"-----------\n"
        f"Files copied   : {stats.files_copied}\n"
        f"Files hardlinked: {stats.files_linked}\n"
        f"Files skipped  : {stats.files_skipped}\n"
        f"Bytes copied   : {fmt_bytes(stats.bytes_copied)}\n"
        f"Errors         : {len(stats.errors)}\n\n"
        f"Configuration\n"
        f"-------------\n"
        f"Retention      : {retention_days} days\n"
        f"Dry run        : {dry_run}\n",
        encoding="utf-8",
    )
    logger.info("Report written: %s", report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Hardlink-based incremental backup for Windows NTFS volumes."
    )
    p.add_argument("--config", default=None,
                   help="Path to backup_config.yaml")
    p.add_argument("--source", action="append", dest="sources", default=[],
                   help="Source directory (repeatable; overrides config)")
    p.add_argument("--dest", default=None,
                   help="Backup destination root (overrides config)")
    p.add_argument("--retention", type=int, default=None,
                   help="Retention days (overrides config)")
    p.add_argument("--no-checksum", action="store_true",
                   help="Skip SHA-256 manifest generation")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be backed up without doing it")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    # Resolve config file
    default_cfg = _PROJECT_ROOT / "backup" / "config" / "backup_config.yaml"
    cfg_path    = Path(args.config) if args.config else default_cfg
    cfg         = load_config(cfg_path) if cfg_path.exists() else {}
    backup_cfg  = cfg.get("backup", {})

    sources        = args.sources or backup_cfg.get("source_dirs", [])
    destination    = args.dest or backup_cfg.get("destination", "")
    retention_days = args.retention or backup_cfg.get("retention_days", 30)
    excludes       = backup_cfg.get("exclude", [])
    gen_checksum   = not args.no_checksum and backup_cfg.get("verify_checksums", True)

    if not sources:
        logger.error("No source directories configured.")
        sys.exit(1)
    if not destination:
        logger.error("No destination configured.")
        sys.exit(1)

    dest_path = Path(destination)
    dest_path.mkdir(parents=True, exist_ok=True)

    timestamp    = datetime.now().strftime(TIMESTAMP_FMT)
    snapshot_dir = dest_path / timestamp
    notifier     = Notifier(cfg.get("notifications", {}))

    logger.info("=== backup_incremental.py started ===")
    logger.info("Sources    : %s", sources)
    logger.info("Destination: %s", dest_path)
    logger.info("Snapshot   : %s", snapshot_dir)
    logger.info("Retention  : %d days", retention_days)
    logger.info("Dry run    : %s", args.dry_run)

    prev_snapshot = get_latest_snapshot(dest_path)
    logger.info("Previous   : %s", prev_snapshot or "none (full backup)")

    log_file = _PROJECT_ROOT / "logs" / f"robocopy_{timestamp}.log"

    if not args.dry_run:
        snapshot_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    agg_stats = BackupStats(0, 0, 0, 0, 0.0, [])

    try:
        with LockFile(LOCK_FILE):
            for source_str in sources:
                source = Path(source_str)
                if not source.exists():
                    logger.warning("Source not found: %s — skipping", source)
                    continue

                logger.info("Backing up: %s", source)

                if is_unc_path(source) or is_unc_path(dest_path):
                    stats = backup_with_robocopy(
                        source, snapshot_dir / source.name,
                        excludes, args.dry_run, log_file
                    )
                else:
                    stats = backup_with_hardlinks(
                        source, snapshot_dir, prev_snapshot,
                        excludes, args.dry_run
                    )

                agg_stats = BackupStats(
                    agg_stats.files_copied  + stats.files_copied,
                    agg_stats.files_linked  + stats.files_linked,
                    agg_stats.files_skipped + stats.files_skipped,
                    agg_stats.bytes_copied  + stats.bytes_copied,
                    0.0,
                    agg_stats.errors + stats.errors,
                )

            elapsed = time.monotonic() - start
            agg_stats = agg_stats._replace(duration_secs=elapsed)

            if not args.dry_run:
                if gen_checksum:
                    generate_manifest(snapshot_dir)

                write_report(
                    snapshot_dir, sources, agg_stats,
                    retention_days, args.dry_run
                )
                update_latest_pointer(dest_path, timestamp)
                rotate_old_snapshots(dest_path, retention_days, args.dry_run)

            logger.info(
                "Backup complete: %d copied, %d hardlinked, %d skipped, %d errors",
                agg_stats.files_copied, agg_stats.files_linked,
                agg_stats.files_skipped, len(agg_stats.errors),
            )

            if agg_stats.errors:
                notifier.send(
                    f"Backup completed with errors: {len(agg_stats.errors)} file(s) failed",
                    body="\n".join(agg_stats.errors[:20]),
                )
            else:
                notifier.send(
                    f"Backup succeeded: {snapshot_dir.name}",
                    body=(
                        f"Copied: {agg_stats.files_copied}  "
                        f"Linked: {agg_stats.files_linked}  "
                        f"Duration: {elapsed:.0f}s"
                    ),
                )

    except Exception as exc:
        logger.error("Backup FAILED: %s", exc)
        notifier.send(
            "BACKUP FAILED",
            body=f"Host: {os.environ.get('COMPUTERNAME', 'unknown')}\nError: {exc}",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
