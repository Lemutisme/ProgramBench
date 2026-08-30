"""Task-conditioned execution policies kept outside ProContract terms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ExecutionPolicyName = Literal["none", "behavioral", "interface", "representation", "reactive", "breadth"]


@dataclass(frozen=True)
class TaskTopology:
    interface_breadth: Literal["narrow", "broad", "massive"]
    state: Literal["stateless", "persistent", "reactive"]
    representation: Literal["text", "structured", "syntax"]
    output: Literal["semantic", "exact"]


POLICIES: dict[ExecutionPolicyName, str] = {
    "none": "",
    "behavioral": (
        "ProgramBench behavioral-search regime v3: enumerate documented behavioral surfaces and use bounded "
        "differential probes to discover material boundary, error, output, and interaction semantics before "
        "coherent edits. Preserve only high-value observations as compact regressions. Delivery replay is not a "
        "behavioral stopping condition; stop when no high-impact discrepancy remains and recent probe batches "
        "discover no new semantic class."
    ),
    "interface": (
        "ProgramBench interface-coverage regime v1: before committing to an architecture, derive a capability "
        "matrix from bundled documentation and help across commands, flags, output modes, persistent state, and "
        "external integrations. Choose available libraries and a modular domain model that represent those axes. "
        "Probe the reference in bounded batches by capability family, comparing exact output, exit status, and "
        "state changes. If failures span families or a fix regresses another family, reconsider the representation "
        "instead of accumulating patches. Preserve high-value probes as compact regressions. Stop after all "
        "high-impact cells have evidence, no material discrepancy remains, and recent batches reveal no new class."
    ),
    "representation": (
        "ProgramBench native-representation regime v1: identify the smallest native semantic representation for "
        "the documented behavior before broad implementation, such as a language parser, syntax tree, or structured "
        "decoder already available in the toolchain. Use discriminating reference probes to establish its "
        "normalization and boundary rules, then implement coherently. Avoid repeated low-yield differential patches "
        "once holdout and metamorphic probes are stable; preserve only regressions that distinguish a semantic rule."
    ),
    "reactive": (
        "ProgramBench reactive-transition regime v1: model the executable as an explicit transition system over "
        "inputs, filesystem events, process state, signals, timers, and observable outputs. Probe transitions and "
        "event order with local 30-60 second bounds, including repeated and adversarial sequences, before repairing "
        "the state machine. Preserve transition witnesses as compact regressions. Stop when the documented "
        "transition graph is covered, no high-impact mismatch remains, and recent batches expose no new state."
    ),
    "breadth": (
        "ProgramBench capability-decomposition regime v1: the interface is too broad for a monolithic imitation. "
        "Partition documented behavior into independently testable capability families, rank them by observable "
        "surface and implementation leverage, and allocate bounded probe and implementation budgets per family. "
        "Build shared parsing and state primitives, then complete the highest-value families end to end instead of "
        "adding shallow special cases across the whole interface. Reallocate when a family saturates; stop when the "
        "remaining families have lower expected score gain than the best unresolved discrepancy."
    ),
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
    return "none"


def policy(name: str) -> str:
    if name not in POLICIES:
        raise ValueError(f"unknown execution policy: {name}")
    return POLICIES[name]
