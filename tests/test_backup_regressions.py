"""Backup regression tests — recursive-inclusion defect (§14).

The 2026-11-03 incident: backups tarred instance/ including its own
backups dir, so each archive contained all previous archives (geometric
growth, 123G). These tests pin the fix: exclusion, no recursive growth,
correct restore, honest failure, retention, atomic publish.
"""

import tarfile
from pathlib import Path

import pytest

from testorchestrator.updater import (BACKUP_RETENTION, UpdateError,
                                      _prune_backups, backup_state,
                                      recover)


def _mk_instance(root: Path) -> Path:
    inst = root / "instance"
    (inst / "backups").mkdir(parents=True)
    (inst / "db.sqlite").write_text("db-content")
    return inst


def test_existing_backups_excluded(tmp_path):
    inst = _mk_instance(tmp_path)
    old = inst / "backups" / "backup-20000101T000000Z.tar.gz"
    old.write_bytes(b"Z" * 10_000)
    out = backup_state(inst / "backups", [inst])
    names = tarfile.open(out).getnames()
    assert "instance/db.sqlite" in names
    assert not any("backups" in n for n in names)


def test_repeated_backups_do_not_grow(tmp_path):
    """N backups -> N small archives, never self-nested growth."""
    inst = _mk_instance(tmp_path)
    sizes = []
    for _ in range(3):
        out = backup_state(inst / "backups", [inst])
        sizes.append(out.stat().st_size)
    assert max(sizes) < 20_000          # all small, no amplification


def test_generated_archive_restores(tmp_path):
    inst = _mk_instance(tmp_path)
    (inst / "certs").mkdir()
    (inst / "certs" / "key.pem").write_text("KEY")
    out = backup_state(inst / "backups", [inst])
    dest = tmp_path / "restore"
    res = recover(out, dest)
    assert (dest / "instance" / "db.sqlite").read_text() == "db-content"
    assert (dest / "instance" / "certs" / "key.pem").is_file()
    assert res["members"] >= 2


def test_failed_backup_not_reported_success(tmp_path, monkeypatch):
    """A backup that dies mid-write raises and leaves no archive that
    looks complete — plus no orphan .part."""
    inst = _mk_instance(tmp_path)

    class BadTar:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, *a, **k): raise OSError("No space left on device")

    monkeypatch.setattr(tarfile, "open", lambda *a, **k: BadTar())
    with pytest.raises(OSError):
        backup_state(inst / "backups", [inst])
    leftovers = list((inst / "backups").iterdir())
    assert leftovers == []               # no .part, no partial archive


def test_truncated_archive_rejected_not_half_restored(tmp_path):
    inst = _mk_instance(tmp_path)
    out = backup_state(inst / "backups", [inst])
    data = out.read_bytes()
    trunc = inst / "backups" / "backup-truncated.tar.gz"
    trunc.write_bytes(data[: len(data) // 3])   # chopped mid-stream
    dest = tmp_path / "restore2"
    with pytest.raises(UpdateError):
        recover(trunc, dest)


def test_retention_keeps_newest_pattern_only(tmp_path):
    bd = tmp_path / "backups"
    bd.mkdir()
    keep = tmp_path / "instance2"
    keep.mkdir()
    (keep / "db").write_text("x")
    # 14 pattern backups + one unrelated file.
    for i in range(14):
        (bd / f"backup-202601{i:02d}T000000Z.tar.gz").write_bytes(b"x")
    (bd / "keepme.txt").write_text("mine")
    removed = _prune_backups(bd)
    survivors = sorted(bd.glob("backup-*.tar.gz"))
    assert removed == 4 and len(survivors) == BACKUP_RETENTION
    assert (bd / "keepme.txt").exists()  # never a pattern-miss casualty
    assert survivors[-1].name == "backup-20260113T000000Z.tar.gz"
    assert survivors[0].name == "backup-20260104T000000Z.tar.gz"
