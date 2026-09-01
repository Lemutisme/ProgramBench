import json

import pytest

from programbench.candidate import package_workspace
from scripts.campaign_probe import replay_archive, validate_report


@pytest.mark.slow
def test_host_replays_typed_corpus_against_reference_and_candidate(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("compile.sh").write_text(
        "#!/bin/sh\nset -eu\nprintf '#!/bin/sh\\nprintf candidate\\n' > executable\nchmod +x executable\n"
    )
    workspace.joinpath("compile.sh").chmod(0o755)
    workspace.joinpath("probes").mkdir()
    workspace.joinpath("probes/cases.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "help",
                        "argv": ["--help"],
                        "stdin": "",
                        "env": {"TERM": "dumb"},
                        "files": [],
                        "timeoutSeconds": 10,
                    }
                ],
            }
        )
    )
    archive = package_workspace(workspace, tmp_path / "run", "mibk__dupl.1bf052b")

    report = replay_archive(archive, "mibk__dupl.1bf052b")

    assert validate_report(report) == report
    assert report["cases"] == 1
    assert report["passed"] is False
    assert report["residuals"][0]["differences"]
