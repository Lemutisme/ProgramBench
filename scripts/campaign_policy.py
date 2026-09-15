"""Opt-in capability policies, applied instead of historical strategy guidance."""

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType


@dataclass(frozen=True)
class PolicyArtifact:
    """Exact execution guidance; contains no Contract terms or promotion authority."""

    text: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip() or "\0" in self.text:
            raise ValueError("policy artifact must contain nonempty UTF-8 text without NUL")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("policy artifact requires a lowercase SHA256")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("policy artifact SHA256 does not match its exact bytes")

    def manifest(self) -> dict[str, object]:
        return {"version": 1, "sha256": self.sha256,
                "utf8_base64": base64.b64encode(self.text.encode("utf-8")).decode("ascii")}


def load_policy_artifact(path: Path, expected_sha256: str) -> PolicyArtifact:
    # This bounds one configuration read, not an instance's execution resources.
    limit = 65536
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("policy artifact must be a regular file of at most 65536 bytes")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("policy artifact exceeds 65536 bytes")
    return PolicyArtifact(data.decode("utf-8"), expected_sha256)


POLICY_TEXT = MappingProxyType(
    {
        "fit": """Choose an implementation language and permitted libraries for the behavior this task requires and what you can implement reliably. Consider the costly semantic boundaries only where relevant: text and bytes, patterns and numbers, or process and terminal I/O. Use task documentation, available toolchains, and allowed reference executions; the original project's language is not automatically the best choice. Prototype a consequential uncertain interface if it could invalidate the design, then build working behavior. Revisit the choice when concrete incompatibilities dominate the implementation, rather than switching merely for novelty.""",
        "search": """When a consequential behavior is uncertain, compare genuinely different algorithms or representations. Use small executable alternatives and inputs on which their predictions disagree to decide what to implement. When an approach repeatedly fails, change the disputed mechanism.

Keep a runnable checkpoint before risky replacements. Compare alternatives under equivalent inputs, environment, and process or file state. Keep reference execution separate from candidate execution, and retain reference stdout/stderr bytes, exit status, relevant effects, and invocation conditions. Expected results come from allowed reference executions or explicit task requirements, never from candidate outputs. If an expectation appears wrong, reobserve the reference and preserve the prior evidence and reason for revision.

Prefer changes that fix reproducible differences while preserving established matches; also examine unresolved documented behavior. Repair or roll back reproducible regressions before discarding the checkpoint. Retain alternatives when the evidence is inconclusive. Candidate agreement, self-test counts, and a few passing probes do not establish complete correctness.""",
    }
)

POLICY_PROFILES = frozenset(POLICY_TEXT)
