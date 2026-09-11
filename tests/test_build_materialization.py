import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from programbench.candidate import PreflightInfrastructureError, package_workspace
from scripts.campaign_freeze import _make_read_only, _snapshot_candidate, _remove_tree
from scripts.campaign_materialize import build_view, semantic_inventory


def setup(tmp_path):
    source = tmp_path / 'source'
    sealed = tmp_path / 'sealed'
    source.mkdir()
    sealed.mkdir()
    (source / 'src').mkdir()
    (source / 'src/generated.h').write_text('old\n')
    (source / 'src/generated.h').chmod(0o644)
    (source / 'readonly-fixture').write_text('intentionally immutable\n')
    (source / 'readonly-fixture').chmod(0o444)
    (source / 'compile.sh').write_text('#!/bin/sh\nset -eu\nprintf rebuilt > src/generated.h\nprintf executable > executable\n')
    (source / 'compile.sh').chmod(0o755)
    _snapshot_candidate(source, sealed)
    _make_read_only(sealed)
    return source, sealed


def test_build_view_preserves_bytes_and_original_modes_without_unsealing_evidence(tmp_path):
    source, sealed = setup(tmp_path)
    before = semantic_inventory(sealed)
    try:
        with build_view(source, sealed, tmp_path) as working:
            assert stat.S_IMODE((working / 'src/generated.h').stat().st_mode) == 0o644
            assert stat.S_IMODE((working / 'readonly-fixture').stat().st_mode) == 0o444
            archive = package_workspace(working, tmp_path / 'out', 'owner__repo.abc')
            with tarfile.open(archive) as tar:
                assert tar.getmember('src').mode & 0o200
                assert tar.getmember('src/generated.h').mode == 0o644
                assert tar.getmember('readonly-fixture').mode == 0o444
            subprocess.run(['./compile.sh'], cwd=working, check=True)
        assert not working.exists()
        assert semantic_inventory(sealed) == before
        assert (source / 'src/generated.h').read_text() == 'old\n'
    finally:
        _remove_tree(sealed)


def test_modified_source_is_not_substituted_for_frozen_evidence(tmp_path):
    source, sealed = setup(tmp_path)
    try:
        (source / 'src/generated.h').write_text('tampered\n')
        with pytest.raises(PreflightInfrastructureError, match='no longer matches'):
            with build_view(source, sealed, tmp_path):
                pytest.fail('must reject before packaging')
        assert list(tmp_path.glob('.build-view.*')) == []
    finally:
        _remove_tree(sealed)
