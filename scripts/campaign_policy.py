"""Opt-in capability policies, applied instead of historical strategy guidance."""

from types import MappingProxyType


POLICY_TEXT = MappingProxyType(
    {
        "fit": """Choose an implementation language and permitted libraries for the behavior this task requires and what you can implement reliably. Consider the costly semantic boundaries only where relevant: text and bytes, patterns and numbers, or process and terminal I/O. Use task documentation, available toolchains, and allowed reference executions; the original project's language is not automatically the best choice. Prototype a consequential uncertain interface if it could invalidate the design, then build working behavior. Revisit the choice when concrete incompatibilities dominate the implementation, rather than switching merely for novelty.""",
        "search": """When a consequential behavior is uncertain, compare genuinely different algorithms or representations. Use small executable alternatives and inputs on which their predictions disagree to decide what to implement. When an approach repeatedly fails, change the disputed mechanism.

Keep a runnable checkpoint before risky replacements. Compare alternatives under equivalent inputs, environment, and process or file state. Keep reference execution separate from candidate execution, and retain reference stdout/stderr bytes, exit status, relevant effects, and invocation conditions. Expected results come from allowed reference executions or explicit task requirements, never from candidate outputs. If an expectation appears wrong, reobserve the reference and preserve the prior evidence and reason for revision.

Prefer changes that fix reproducible differences while preserving established matches; also examine unresolved documented behavior. Repair or roll back reproducible regressions before discarding the checkpoint. Retain alternatives when the evidence is inconclusive. Candidate agreement, self-test counts, and a few passing probes do not establish complete correctness.""",
    }
)

POLICY_PROFILES = frozenset(POLICY_TEXT)
