#!/usr/bin/env python3
"""Publish one immutable candidate artifact for every stopped campaign workspace."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.campaign_runner import CampaignJob, _campaign_name, _conserve_candidate_artifact
from scripts.campaign_state import JobPaths, StateStore, job_lock
from scripts.campaign_workspace import WorkspaceManifest


class SalvageError(RuntimeError):
    pass


def salvage_campaign(root: Path) -> Mapping[str, object]:
    campaign = Path(root).resolve(strict=True)
    manifest = _read_json(campaign / "campaign.json")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise SalvageError("campaign manifest has no jobs")
    candidates = tuple(_job(item) for item in jobs)
    if len(candidates) != len({candidate.key for candidate in candidates}):
        raise SalvageError("campaign manifest contains duplicate jobs")
    with job_lock(campaign / ".run-all.lock"):
        _require_no_running_containers(campaign)
        outcomes = {"published": 0, "failed": 0}
        for candidate in candidates:
            paths = JobPaths(campaign / candidate.arm / candidate.instance_id)
            with job_lock(paths.lock):
                state = StateStore(paths.state).read()
                workspace = WorkspaceManifest.from_dict(_read_json(paths.root / "workspace-state/workspace.json"))
                if (
                    workspace.campaign != _campaign_name(campaign)
                    or workspace.instance_id != candidate.instance_id
                    or workspace.arm != candidate.arm
                    or workspace.job_root != paths.root
                    or workspace.phase != "stopped"
                ):
                    raise SalvageError(f"workspace is not an exact stopped campaign job: {candidate.instance_id}")
                published = _conserve_candidate_artifact(
                    paths,
                    StateStore(paths.state),
                    SimpleNamespace(candidate=workspace.candidate),
                    candidate,
                    state.exit_class or state.phase,
                )
                outcomes["published" if published else "failed"] += 1
        return {"campaign": str(campaign), "jobs": len(candidates), **outcomes}


def _job(raw: object) -> CampaignJob:
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("instance_id"), str)
        or not isinstance(raw.get("arm"), str)
        or not isinstance(raw.get("image"), str)
    ):
        raise SalvageError("campaign manifest contains an invalid job")
    return CampaignJob(str(raw["instance_id"]), str(raw["arm"]), str(raw["image"]))


def _require_no_running_containers(root: Path) -> None:
    environment = {
        name: value
        for name in ("PATH", "DOCKER_HOST", "LANG", "LC_ALL")
        if (value := os.environ.get(name))
    }
    result = subprocess.run(
        (
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=programbench.campaign={_campaign_name(root)}",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise SalvageError("could not verify campaign containers")
    if result.stdout.strip():
        raise SalvageError("campaign still has running containers")


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise SalvageError(f"could not read campaign artifact: {path}") from error
    if not isinstance(value, Mapping):
        raise SalvageError(f"campaign artifact is not an object: {path}")
    return value


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish candidate artifacts after a stopped campaign")
    parser.add_argument("--campaign", type=Path, required=True)
    args = parser.parse_args(arguments)
    print(json.dumps(salvage_campaign(args.campaign), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
