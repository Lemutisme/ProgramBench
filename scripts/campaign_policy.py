"""Task-conditioned execution policies kept outside ProContract terms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ExecutionPolicyName = Literal[
    "none",
    "behavioral",
    "interface",
    "representation",
    "reactive",
    "breadth",
    "fast",
    "deep",
    "v2",
    "v2-probe",
]


@dataclass(frozen=True)
class TaskTopology:
    interface_breadth: Literal["narrow", "broad", "massive"]
    state: Literal["stateless", "persistent", "reactive"]
    representation: Literal["text", "structured", "syntax"]
    output: Literal["semantic", "exact"]


CAPABILITY_CONSERVATION = (
    "ProgramBench capability-conservation layer v1: maintain a live matrix across documented commands, flags, "
    "input discovery, errors, output modes, environment, persistent state, temporal transitions, and integrations. "
    "Every reference/candidate discrepancy that changes an implementation decision, and every repaired behavior, "
    "becomes a compact regression witness. After coherent edits replay all retained witnesses; a candidate that "
    "loses one cannot replace the incumbent. Treat delivery replay as packaging evidence, not capability saturation. "
    "Stop only when all documented high-impact cells have evidence, no material discrepancy remains, and recent "
    "holdout or metamorphic probes reveal no new class."
)

PROBE_CORPUS_POLICY = (
    " Before handoff create probes/cases.json as {\"version\":1,\"cases\":[...]}. Each answer-free case has "
    "id, argv (arguments only), optional stdin/env/files/timeoutSeconds; each fixture file has path, content, and "
    "optional executable. Do not store expected outputs. The host will run the frozen cases independently against "
    "reference and candidate and will reject any exit/stdout/stderr/timeout residual."
)

V2_CAPABILITY_POLICY = (
    "ProgramBench delivery policy v2: enumerate documented surfaces, boundary and error semantics, and material "
    "interactions; use batched differential probes within the action deadline, then preserve observations as "
    "regression checks after coherent edits. Delivery replay alone is not a behavioral stopping condition. Hand "
    "off one exact frozen candidate snapshot containing compile.sh, executable, and a self-contained validate.sh "
    "regression suite after native replay runs compile.sh with package-manager network access disabled and then "
    "validate.sh, both exiting 0 within 600 seconds. validate.sh must encode behavioral evidence learned from "
    "black-box reference experiments and must not access the reference, network, or ambient package caches during "
    "replay. Before handoff, broadly probe every applicable documented command, flag, error, boundary class, and "
    "state transition; continue until no material behavioral gap remains or the shared budget is exhausted. The "
    "ProgramBench harness performs packaging and preflight only; official benchmark evaluation remains external."
)

POLICIES: dict[ExecutionPolicyName, str] = {
    "none": "",
    "behavioral": CAPABILITY_CONSERVATION
    + " Use bounded differential batches to discover cross-cutting invariants before coherent implementation edits.",
    "interface": CAPABILITY_CONSERVATION
    + (
        " Interface prior: derive a command/flag/output/state/integration matrix before choosing the architecture. "
        "Use available libraries and a modular domain model that represent those axes. If mismatches span families "
        "or a fix regresses another family, reconsider the representation instead of accumulating patches."
    ),
    "representation": CAPABILITY_CONSERVATION
    + (
        " Representation prior: identify the smallest native semantic representation, such as a parser, syntax "
        "tree, or structured decoder already available in the toolchain. Establish its normalization rules with "
        "discriminating probes, then implement coherently without neglecting the surrounding behavioral envelope."
    ),
    "reactive": CAPABILITY_CONSERVATION
    + (
        " Reactive prior: model inputs, filesystem events, process state, signals, timers, and outputs as an explicit "
        "transition system. Probe event order with local 30-60 second bounds, including repeated and adversarial "
        "sequences, while retaining CLI, environment, directory, and error witnesses outside the transition core."
    ),
    "breadth": CAPABILITY_CONSERVATION
    + (
        " Breadth prior: partition the interface into independently testable capability families and allocate bounded "
        "probe and implementation budgets by observable surface and implementation leverage. Build shared primitives, "
        "then complete high-value families end to end rather than adding shallow cases across the whole interface."
    ),
    "fast": CAPABILITY_CONSERVATION
    + (
        " Fast lane: batch a small discriminating probe basis, choose the strongest available semantic primitive, "
        "then make one coherent implementation early and reserve the remaining budget for residual repair. Maintain "
        "answer-free typed cases for host replay. Rebuild from a fresh checkout before handoff."
    )
    + PROBE_CORPUS_POLICY,
    "deep": CAPABILITY_CONSERVATION
    + (
        " Deep lane: establish a broad behavioral basis before implementation, choose mature semantic primitives, "
        "and organize the implementation by capability family. Maintain probes/cases.json as answer-free typed cases "
        "for host replay; use differential fuzz and residual clusters for targeted repair, re-expanding the basis "
        "when failures cross families. Perform multiple fresh rebuilds before handoff."
    )
    + PROBE_CORPUS_POLICY,
    "v2": V2_CAPABILITY_POLICY,
    "v2-probe": V2_CAPABILITY_POLICY + PROBE_CORPUS_POLICY,
}


def select_policy(topology: TaskTopology) -> ExecutionPolicyName:
    if topology.interface_breadth == "massive":
        return "breadth"
    if topology.state == "reactive":
        return "reactive"
    if topology.interface_breadth == "broad" and (topology.state == "persistent" or topology.output == "exact"):
        return "interface"
    if topology.representation in {"structured", "syntax"}:
        return "representation"
    return "behavioral"


def policy(name: str) -> str:
    if name not in POLICIES:
        raise ValueError(f"unknown execution policy: {name}")
    return POLICIES[name]
