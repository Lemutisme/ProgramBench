import stat
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from programbench.candidate import PreflightInfrastructureError, package_workspace
from scripts.campaign_freeze import _make_read_only, _snapshot_candidate, _remove_tree
from scripts.campaign_materialize import build_view, semantic_inventory
from scripts.campaign_procontract import _materialize_subject


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


@pytest.mark.parametrize('mask', [0o022, 0o077])
def test_snapshot_retains_nested_and_read_only_directory_modes_under_umask(tmp_path, mask):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'src/nested').mkdir(parents=True)
    (source / 'fixtures').mkdir()
    (source / 'src/nested/file').write_text('source\n')
    (source / 'fixtures/frozen').write_text('immutable\n')
    (source / 'src').chmod(0o755)
    (source / 'src/nested').chmod(0o711)
    (source / 'fixtures/frozen').chmod(0o444)
    (source / 'fixtures').chmod(0o500)
    original = semantic_inventory(source)
    sealed = tmp_path / 'sealed'
    sealed.mkdir()
    previous = os.umask(mask)
    try:
        _snapshot_candidate(source, sealed)
        assert semantic_inventory(sealed) == original
        _make_read_only(sealed)
        frozen = semantic_inventory(sealed)
        with build_view(source, sealed, tmp_path) as working:
            assert semantic_inventory(working) == original
            assert stat.S_IMODE((working / 'fixtures').stat().st_mode) == 0o500
            assert (working / 'fixtures/frozen').read_text() == 'immutable\n'
        assert semantic_inventory(source) == original
        assert semantic_inventory(sealed) == frozen
    finally:
        os.umask(previous)
        _remove_tree(sealed)
        _remove_tree(source)


def test_native_subject_and_build_copy_match_under_private_controller_umask(tmp_path):
    source = tmp_path / 'export'
    (source / 'nested').mkdir(parents=True)
    (source / 'nested').chmod(0o755)
    (source / 'nested/source.txt').write_text('preserve me')
    original = semantic_inventory(source)
    subject = tmp_path / 'subject'
    previous = os.umask(0o077)
    try:
        _materialize_subject(source, subject, tmp_path, ())
        with build_view(source, subject, tmp_path) as working:
            assert semantic_inventory(working) == original
        _materialize_subject(source, subject, tmp_path, ())
        assert semantic_inventory(source) == original
        assert semantic_inventory(source, read_only=True) == semantic_inventory(subject)
    finally:
        os.umask(previous)
        if subject.exists():
            _remove_tree(subject)
