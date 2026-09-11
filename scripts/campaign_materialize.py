"""Package a private build view, never the permissions of a read-only evidence store."""

import tempfile
from contextlib import contextmanager
from pathlib import Path

from programbench.candidate import PreflightInfrastructureError
from scripts.campaign_freeze import _inventory, _remove_tree, _snapshot_candidate


CONTROL_PATHS = ('.git', '.programbench', '.programbench-cleanroom.json', 'reference')


def semantic_inventory(root, *, read_only=False):
    return [
        {key: value for key, value in item.items()
         if key not in {'device', 'inode'} and not (index == 0 and key == 'mode')}
        for index, item in enumerate(_inventory(root, excluded=CONTROL_PATHS, read_only_modes=read_only))
    ]


@contextmanager
def build_view(source: Path, sealed: Path, parent: Path):
    """Restore from the original materialization, retaining intentionally read-only files.

    The source must still match the sealed bytes and executable bits. We do not
    guess missing write bits or chmod the evidence tree. Callers retain both
    trees; this context owns only its newly-created temporary copy.
    """
    original = semantic_inventory(source)
    protected = semantic_inventory(sealed)
    if semantic_inventory(source, read_only=True) != protected:
        raise PreflightInfrastructureError('Build source no longer matches the frozen evidence')
    temporary = Path(tempfile.mkdtemp(prefix='.build-view.', dir=parent))
    try:
        _snapshot_candidate(source, temporary)
        if semantic_inventory(temporary) != original or semantic_inventory(source) != original:
            raise PreflightInfrastructureError('Build materialization changed during capture')
        yield temporary
        if semantic_inventory(source) != original or semantic_inventory(sealed) != protected:
            raise PreflightInfrastructureError('Source/evidence changed during packaging')
    finally:
        _remove_tree(temporary)
