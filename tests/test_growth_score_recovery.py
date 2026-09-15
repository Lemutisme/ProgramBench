from pathlib import Path
import os
import sys

import pytest

from scripts.growth_loop import GrowthError, read, sha256
from scripts.recover_growth_scoring import copied, execute, fingerprint, identity, prepare


def test_opaque_copy_preserves_original_bytes_and_prevents_replacement(tmp_path):
    source = tmp_path / "old/archive"
    source.parent.mkdir()
    source.write_bytes(b"opaque frozen archive")
    before = source.stat()
    target = tmp_path / "new/archive"
    reference = copied(source, target)
    assert reference == {"path": str(source), "copy": str(target), "sha256": sha256(source.read_bytes())}
    assert target.read_bytes() == source.read_bytes()
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert source.stat().st_mode == before.st_mode
    assert source.stat().st_ino != target.stat().st_ino
    source.write_bytes(b"different archive")
    with pytest.raises(GrowthError, match="immutable record conflicts"):
        copied(source, target)
    assert target.read_bytes() == b"opaque frozen archive"


def test_recovery_rejects_symlink_input_and_parent(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    (source / "file").write_bytes(b"data")
    (tmp_path / "alias").symlink_to(source, target_is_directory=True)
    (source / "link").symlink_to(source / "file")
    for path in [source / "link", tmp_path / "alias/file", Path("relative")]:
        with pytest.raises(GrowthError, match="canonical regular file"):
            fingerprint(path)


@pytest.mark.parametrize("location", ["old", "old/recovery", "."])
def test_recovery_cannot_prepare_within_or_over_original_study(tmp_path, location):
    source = tmp_path / "old"
    source.mkdir()
    with pytest.raises(GrowthError, match="new output outside"):
        prepare(source, tmp_path / "unused-panel", tmp_path / location)
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_child_identity_and_exit_receipts_survive_cancellation(tmp_path, cancel):
    argv = [sys.executable, "-c", "print('completed')"]
    if cancel:
        argv = [sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('cancel'); time.sleep(30)",
                str(tmp_path / "CANCEL")]
        with pytest.raises(KeyboardInterrupt):
            execute(tmp_path, "child", argv, tmp_path, {"PATH": os.defpath})
    if not cancel:
        assert execute(tmp_path, "child", argv, tmp_path, {"PATH": os.defpath}) == 0
    started = read(tmp_path / "child-process.json")
    ended = read(tmp_path / "child-exit.json")
    assert started["identity"] == ended["identity"]
    assert started["argv"] == ended["argv"] == argv
    assert started["identity"]["pid"] == started["identity"]["pgid"]
    assert started["identity"]["start_ticks"] > 0
    assert started["identity"]["boot_id"] == identity(os.getpid())["boot_id"]
    assert not Path(f'/proc/{started["identity"]["pid"]}').exists()
    assert ended["completion"] == ("interrupted" if cancel else "completed")
    assert ended["cancel_marker_retained"] is cancel
    assert (tmp_path / "CANCEL").exists() is cancel
