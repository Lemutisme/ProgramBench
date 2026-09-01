import json

import pytest

from scripts.campaign_probe import Observation, ProbeError, case_hash, compare, load_corpus, validate_report


def corpus() -> bytes:
    return json.dumps(
        {
            "version": 1,
            "cases": [
                {
                    "id": "help",
                    "argv": ["--help"],
                    "stdin": "",
                    "env": {"TERM": "dumb"},
                    "files": [{"path": "input.txt", "content": "hello\n"}],
                    "timeoutSeconds": 5,
                }
            ],
        }
    ).encode()


def test_canonicalizes_answer_free_probe_cases() -> None:
    digest, cases = load_corpus(corpus())

    assert len(digest) == 64
    assert cases[0].id == "help"
    assert cases[0].env == (("TERM", "dumb"),)
    assert cases[0].files[0].path == "input.txt"


def test_rejects_escape_paths_credentials_and_duplicate_ids() -> None:
    raw = json.loads(corpus())
    raw["cases"][0]["files"][0]["path"] = "../escape"
    with pytest.raises(ProbeError, match="path"):
        load_corpus(json.dumps(raw).encode())
    raw = json.loads(corpus())
    raw["cases"][0]["env"] = {"API_KEY": "secret"}
    with pytest.raises(ProbeError, match="environment"):
        load_corpus(json.dumps(raw).encode())
    raw = json.loads(corpus())
    raw["cases"].append(raw["cases"][0])
    with pytest.raises(ProbeError, match="unique"):
        load_corpus(json.dumps(raw).encode())


def test_reports_exact_residuals_without_comparing_duration() -> None:
    digest, cases = load_corpus(corpus())

    def run(case, target):
        return Observation(
            case_hash=case_hash(case),
            target=target,
            exit_code=0,
            stdout="cmVmZXJlbmNl" if target == "reference" else "Y2FuZGlkYXRl",
            stderr="",
            duration_ms=1 if target == "reference" else 999,
            timed_out=False,
        )

    report = compare(digest, cases, run)

    assert report["passed"] is False
    assert report["residuals"] == [
        {"caseID": "help", "caseHash": case_hash(cases[0]), "differences": ["stdout"]}
    ]
    assert len(report["reportHash"]) == 64
    assert validate_report(report) == report
    report["passed"] = True
    with pytest.raises(ProbeError, match="inconsistent"):
        validate_report(report)
