# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import typer

from programbench.candidate import (
    DEFAULT_IMAGE_TAG,
    CandidateError,
    clone_workspace,
    package_workspace,
    preflight_candidate,
    retain_regressions,
    sha256,
    verify_probes,
)
from programbench.constants import DOCKER_CPUS

app = typer.Typer(no_args_is_help=True, add_completion=False)
Value = TypeVar("Value")


@app.command()
def clone(
    source: Path = typer.Argument(..., exists=True, file_okay=False),
    destination: Path = typer.Argument(...),
) -> None:
    """Clone one frozen first-pass workspace for remediation."""
    _run(lambda: clone_workspace(source, destination))
    typer.echo(destination)


@app.command("package")
def package_command(
    workspace: Path = typer.Argument(..., exists=True, file_okay=False),
    run_dir: Path = typer.Argument(...),
    instance_id: str = typer.Argument(...),
) -> None:
    """Create a deterministic submission.tar.gz without reference artifacts."""
    archive = _run(lambda: package_workspace(workspace, run_dir, instance_id))
    typer.echo(f"{archive}  sha256={sha256(archive)}")


@app.command()
def preflight(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    instance_id: str = typer.Argument(...),
    image_tag: str = typer.Option(DEFAULT_IMAGE_TAG, "--image-tag"),
    docker_cpus: int = typer.Option(DOCKER_CPUS, "--docker-cpus"),
) -> None:
    """Compile a frozen archive in the same cleanroom used by evaluation."""
    archive = run_dir / instance_id / "submission.tar.gz"
    result = _run(lambda: preflight_candidate(archive, instance_id, image_tag=image_tag, docker_cpus=docker_cpus))
    typer.echo(f"preflight passed  executable_sha256={result.executable_hash}")


@app.command()
def probe(
    cases: Path = typer.Argument(..., exists=True, dir_okay=False),
    ledger: Path = typer.Argument(...),
    reference: Path = typer.Argument(..., exists=True, dir_okay=False),
    candidate: Path = typer.Argument(..., exists=True, dir_okay=False),
    timeout: float = typer.Option(5, "--timeout", min=0.01),
) -> None:
    """Freeze reference probes and reject candidate regressions."""
    total, mismatches = _run(lambda: verify_probes(cases, ledger, reference, candidate, timeout=timeout))
    typer.echo(f"probes={total} mismatches={mismatches}")
    if mismatches:
        raise typer.Exit(1)


@app.command()
def retain(
    cases: Path = typer.Argument(..., exists=True, dir_okay=False),
    ledger: Path = typer.Argument(...),
    reference: Path = typer.Argument(..., exists=True, dir_okay=False),
    predecessor: Path = typer.Argument(..., exists=True, dir_okay=False),
    candidate: Path = typer.Argument(..., exists=True, dir_okay=False),
    timeout: float = typer.Option(5, "--timeout", min=0.01),
) -> None:
    """Retain repaired counterexamples and replay every retained regression."""
    result = _run(lambda: retain_regressions(cases, ledger, reference, predecessor, candidate, timeout=timeout))
    typer.echo(
        f"retained={result.retained} added={result.added} "
        f"mismatches={result.mismatches} head_sha256={result.head_sha256}"
    )
    if result.mismatches:
        raise typer.Exit(1)


def _run(operation: Callable[[], Value]) -> Value:
    try:
        return operation()
    except CandidateError as error:
        raise typer.BadParameter(str(error)) from error
