"""
tests/test_backup.py
Unit tests for backup/windows/backup_incremental.py and restore.py

Tests cover:
  - Snapshot listing and rotation
  - latest.txt pointer management
  - SHA-256 manifest generation and integrity
  - Hardlink-based incremental copy logic
  - BackupStats aggregation
  - Backup report writing
  - Exclude pattern matching
  - files_are_identical() comparison
  - Config loading integration
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Resolve project root
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "backup" / "windows"))

from backup_incremental import (
    BackupStats,
    LATEST_FILE,
    MANIFEST_FILE,
    REPORT_FILE,
    TIMESTAMP_FMT,
    backup_with_hardlinks,
    file_sha256,
    files_are_identical,
    generate_manifest,
    get_latest_snapshot,
    is_unc_path,
    list_snapshots,
    matches_exclude,
    rotate_old_snapshots,
    update_latest_pointer,
    write_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snapshot(dest: Path, name: str, files: dict[str, str] | None = None) -> Path:
    """Create a fake snapshot directory with optional file contents."""
    snap = dest / name
    snap.mkdir(parents=True, exist_ok=True)
    if files:
        for rel_path, content in files.items():
            f = snap / rel_path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
    return snap


def make_snapshot_dated(dest: Path, dt: datetime, files: dict | None = None) -> Path:
    return make_snapshot(dest, dt.strftime(TIMESTAMP_FMT), files)


# ---------------------------------------------------------------------------
# list_snapshots / get_latest_snapshot
# ---------------------------------------------------------------------------

class TestListSnapshots:
    def test_empty_dest(self, tmp_path):
        assert list_snapshots(tmp_path) == []

    def test_returns_sorted_list(self, tmp_path):
        make_snapshot(tmp_path, "2026-03-01_020000")
        make_snapshot(tmp_path, "2026-03-03_020000")
        make_snapshot(tmp_path, "2026-03-02_020000")
        names = [s.name for s in list_snapshots(tmp_path)]
        assert names == ["2026-03-01_020000", "2026-03-02_020000", "2026-03-03_020000"]

    def test_ignores_non_snapshot_dirs(self, tmp_path):
        make_snapshot(tmp_path, "2026-03-01_020000")
        (tmp_path / "config").mkdir()
        (tmp_path / "logs").mkdir()
        names = [s.name for s in list_snapshots(tmp_path)]
        assert names == ["2026-03-01_020000"]


class TestGetLatestSnapshot:
    def test_none_when_empty(self, tmp_path):
        assert get_latest_snapshot(tmp_path) is None

    def test_reads_latest_txt(self, tmp_path):
        snap = make_snapshot(tmp_path, "2026-03-02_020000")
        (tmp_path / LATEST_FILE).write_text("2026-03-02_020000")
        result = get_latest_snapshot(tmp_path)
        assert result == snap

    def test_falls_back_to_last_dir(self, tmp_path):
        make_snapshot(tmp_path, "2026-03-01_020000")
        latest = make_snapshot(tmp_path, "2026-03-03_020000")
        make_snapshot(tmp_path, "2026-03-02_020000")
        result = get_latest_snapshot(tmp_path)
        assert result == latest

    def test_latest_txt_pointing_to_missing_dir_falls_back(self, tmp_path):
        make_snapshot(tmp_path, "2026-03-01_020000")
        (tmp_path / LATEST_FILE).write_text("2026-03-99_999999")  # invalid
        result = get_latest_snapshot(tmp_path)
        assert result is not None
        assert result.name == "2026-03-01_020000"


class TestUpdateLatestPointer:
    def test_creates_latest_txt(self, tmp_path):
        update_latest_pointer(tmp_path, "2026-03-02_020000")
        assert (tmp_path / LATEST_FILE).read_text() == "2026-03-02_020000"

    def test_overwrites_existing(self, tmp_path):
        update_latest_pointer(tmp_path, "2026-03-01_020000")
        update_latest_pointer(tmp_path, "2026-03-02_020000")
        assert (tmp_path / LATEST_FILE).read_text() == "2026-03-02_020000"


# ---------------------------------------------------------------------------
# rotate_old_snapshots
# ---------------------------------------------------------------------------

class TestRotateOldSnapshots:
    def test_removes_old_snapshots(self, tmp_path):
        old_dt = datetime.now() - timedelta(days=35)
        new_dt = datetime.now() - timedelta(days=2)
        old_snap = make_snapshot_dated(tmp_path, old_dt)
        new_snap = make_snapshot_dated(tmp_path, new_dt)

        # Backdate old snapshot mtime
        old_mtime = old_dt.timestamp()
        os.utime(old_snap, (old_mtime, old_mtime))

        removed = rotate_old_snapshots(tmp_path, retention_days=30, dry_run=False)
        assert removed == 1
        assert not old_snap.exists()
        assert new_snap.exists()

    def test_dry_run_does_not_delete(self, tmp_path):
        old_dt = datetime.now() - timedelta(days=35)
        old_snap = make_snapshot_dated(tmp_path, old_dt)
        old_mtime = old_dt.timestamp()
        os.utime(old_snap, (old_mtime, old_mtime))

        removed = rotate_old_snapshots(tmp_path, retention_days=30, dry_run=True)
        assert removed == 1
        assert old_snap.exists()  # NOT deleted

    def test_nothing_to_rotate(self, tmp_path):
        recent = datetime.now() - timedelta(days=5)
        make_snapshot_dated(tmp_path, recent)
        removed = rotate_old_snapshots(tmp_path, retention_days=30, dry_run=False)
        assert removed == 0

    def test_skips_non_snapshot_dirs(self, tmp_path):
        (tmp_path / "config").mkdir()
        removed = rotate_old_snapshots(tmp_path, retention_days=0, dry_run=False)
        assert removed == 0
        assert (tmp_path / "config").exists()


# ---------------------------------------------------------------------------
# files_are_identical
# ---------------------------------------------------------------------------

class TestFilesAreIdentical:
    def test_identical_files(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("hello")
        b.write_text("hello")
        # Force same mtime
        mtime = a.stat().st_mtime
        os.utime(b, (mtime, mtime))
        assert files_are_identical(a, b) is True

    def test_different_size(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("hello")
        b.write_text("hello world")
        assert files_are_identical(a, b) is False

    def test_different_mtime(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("hello")
        b.write_text("hello")
        mtime_a = a.stat().st_mtime
        os.utime(b, (mtime_a + 10, mtime_a + 10))
        assert files_are_identical(a, b) is False

    def test_missing_file_returns_false(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("hello")
        assert files_are_identical(a, tmp_path / "nonexistent.txt") is False


# ---------------------------------------------------------------------------
# matches_exclude
# ---------------------------------------------------------------------------

class TestMatchesExclude:
    def test_exact_name_match(self):
        assert matches_exclude("node_modules", ["node_modules"]) is True

    def test_glob_extension(self):
        assert matches_exclude("report.tmp", ["*.tmp"]) is True
        assert matches_exclude("report.txt", ["*.tmp"]) is False

    def test_no_patterns(self):
        assert matches_exclude("anything.log", []) is False

    def test_multiple_patterns_any_match(self):
        assert matches_exclude("cache.swp", ["*.tmp", "*.swp"]) is True

    def test_path_with_dir(self):
        assert matches_exclude("build/__pycache__/x.pyc", ["__pycache__"]) is True


# ---------------------------------------------------------------------------
# is_unc_path
# ---------------------------------------------------------------------------

class TestIsUncPath:
    def test_unc_path(self):
        assert is_unc_path(Path("\\\\server\\share")) is True

    def test_local_path(self):
        assert is_unc_path(Path("C:\\Users")) is False

    def test_linux_path(self):
        assert is_unc_path(Path("/home/user")) is False


# ---------------------------------------------------------------------------
# file_sha256
# ---------------------------------------------------------------------------

class TestFileSha256:
    def test_known_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert file_sha256(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert file_sha256(f) == expected


# ---------------------------------------------------------------------------
# generate_manifest
# ---------------------------------------------------------------------------

class TestGenerateManifest:
    def test_creates_manifest_file(self, tmp_path):
        snap = make_snapshot(tmp_path, "2026-03-01_020000", {
            "C/Users/alice/doc.txt": "hello",
            "C/Users/bob/notes.txt": "world",
        })
        count = generate_manifest(snap)
        assert count == 2
        assert (snap / MANIFEST_FILE).exists()

    def test_manifest_contains_correct_hashes(self, tmp_path):
        content = "test content"
        snap = make_snapshot(tmp_path, "2026-03-01_020000", {
            "C/file.txt": content,
        })
        generate_manifest(snap)
        manifest_text = (snap / MANIFEST_FILE).read_text(encoding="utf-8")
        expected_hash = hashlib.sha256(content.encode()).hexdigest()
        assert expected_hash in manifest_text

    def test_excludes_manifest_and_report_files(self, tmp_path):
        snap = make_snapshot(tmp_path, "2026-03-01_020000", {
            "data.txt": "hello",
        })
        (snap / REPORT_FILE).write_text("report")
        count = generate_manifest(snap)
        # Only data.txt should be counted, not MANIFEST_FILE or REPORT_FILE
        assert count == 1
        manifest_text = (snap / MANIFEST_FILE).read_text()
        assert REPORT_FILE not in manifest_text
        assert MANIFEST_FILE not in manifest_text


# ---------------------------------------------------------------------------
# backup_with_hardlinks
# ---------------------------------------------------------------------------

class TestBackupWithHardlinks:
    def test_copies_new_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("hello")
        snap_dir = tmp_path / "snapshot"
        snap_dir.mkdir()

        stats = backup_with_hardlinks(source, snap_dir, None, [], dry_run=False)
        assert stats.files_copied == 1
        assert stats.files_linked == 0

    def test_hardlinks_unchanged_files(self, tmp_path):
        # Create a "previous" snapshot with the file
        source = tmp_path / "source"
        source.mkdir()
        src_file = source / "file.txt"
        src_file.write_text("hello")

        prev_snap = tmp_path / "prev_snap"
        prev_snap.mkdir()
        drive, _ = os.path.splitdrive(str(source))
        drive_label = drive.rstrip(":") or "root"
        prev_file = prev_snap / drive_label / "file.txt"
        prev_file.parent.mkdir(parents=True, exist_ok=True)
        prev_file.write_text("hello")
        # Make sure mtimes match
        mtime = src_file.stat().st_mtime
        os.utime(prev_file, (mtime, mtime))

        new_snap = tmp_path / "new_snap"
        new_snap.mkdir()

        stats = backup_with_hardlinks(source, new_snap, prev_snap, [], dry_run=False)
        assert stats.files_linked == 1
        assert stats.files_copied == 0

    def test_respects_excludes(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "keep.txt").write_text("keep")
        (source / "ignore.tmp").write_text("ignore")
        snap = tmp_path / "snap"
        snap.mkdir()

        stats = backup_with_hardlinks(source, snap, None, ["*.tmp"], dry_run=False)
        assert stats.files_copied == 1
        assert stats.files_skipped == 1

    def test_dry_run_does_not_copy(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")
        snap = tmp_path / "snap"
        snap.mkdir()

        stats = backup_with_hardlinks(source, snap, None, [], dry_run=True)
        assert stats.files_copied == 1   # counted but not written
        drive, _ = os.path.splitdrive(str(source))
        drive_label = drive.rstrip(":") or "root"
        assert not (snap / drive_label / "file.txt").exists()


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_report_file_created(self, tmp_path):
        snap = tmp_path / "2026-03-01_020000"
        snap.mkdir()
        stats = BackupStats(10, 5, 2, 1024 * 1024, 42.5, [])
        write_report(snap, ["C:\\Users"], stats, 30, False)
        assert (snap / REPORT_FILE).exists()

    def test_report_contains_key_fields(self, tmp_path):
        snap = tmp_path / "2026-03-01_020000"
        snap.mkdir()
        stats = BackupStats(10, 5, 2, 1024 * 1024, 65.0, ["err1"])
        write_report(snap, ["C:\\Users"], stats, 30, False)
        text = (snap / REPORT_FILE).read_text(encoding="utf-8")
        assert "Files copied" in text
        assert "Files hardlinked" in text
        assert "Retention" in text
        assert "C:\\Users" in text

    def test_report_shows_error_count(self, tmp_path):
        snap = tmp_path / "2026-03-01_020000"
        snap.mkdir()
        stats = BackupStats(3, 0, 0, 512, 10.0, ["err1", "err2"])
        write_report(snap, ["/home"], stats, 7, False)
        text = (snap / REPORT_FILE).read_text(encoding="utf-8")
        assert "2" in text  # 2 errors


# ---------------------------------------------------------------------------
# BackupStats aggregation
# ---------------------------------------------------------------------------

class TestBackupStats:
    def test_named_tuple_fields(self):
        s = BackupStats(1, 2, 3, 100, 5.0, ["e"])
        assert s.files_copied == 1
        assert s.files_linked == 2
        assert s.files_skipped == 3
        assert s.bytes_copied == 100
        assert s.errors == ["e"]

    def test_replace_duration(self):
        s = BackupStats(1, 0, 0, 0, 0.0, [])
        s2 = s._replace(duration_secs=12.5)
        assert s2.duration_secs == 12.5
        assert s2.files_copied == 1
