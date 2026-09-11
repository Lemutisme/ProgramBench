# Requested policy-split 3108ea519 pilot

The target is the committed `contract-policy-split` version
`3108ea519a59d79f2f18f740221331fe6f83a44d` (`test(core): pin contract execution
continuity invariants`), not the dirty main worktree. The detached source worktree
is `/home/duozhou/opencode-3108-pilot`.

This commit predates the network-none provider transport. The runtime coordinate
therefore includes a separately frozen, model-only Unix transport adapter copied
from the qualified offline implementation. Runtime changes are limited to
`packages/core/src/effect/app-node-platform.ts` and the new `provider-transport.ts`;
the corresponding test is also retained. Contract, Session, tool, Schema, and
behavioral policy sources remain those of the requested commit. Do not label
the binary as an entirely unmodified build of 3108ea519.

- Binary SHA-256: `58a1358179a528a2a32da9eadfe67a088c313702c2e8fc3be8a8c937972cef1f`.
- Overlay coordinate SHA-256: `5b08a481e020189033486d378658f8f9f359c69a320f09943cbae8f4751f8219`.
- Version: `0.0.0-policy3108-20260911`; Bun 1.3.14, Linux x64.
- Same frozen model catalog as the grounded pilot:
  `105288924be2dc7ebf7abda00dc064d710797a09811708329232518d515a8410`.

## Runner fixes necessary for qualification

This runner branch `pilot-delivery` starts at `3514384`. It fixes the two errors
found during the grounded pilot without modifying those experiments:

1. Snapshot copying creates private writable directories while populating them,
   then restores each original directory mode bottom-up. This preserves `0755`
   under controller umask `077` and permits copying an intentionally read-only
   subtree. Native subject publication now verifies the copied inventory before
   accepting the sealed tree.
2. Artifact indexing prunes excluded runtime/candidate/binary trees before
   descent. It does not chmod or delete inaccessible experiment data.

The earlier failed sealed trees remain unchanged: they already contain the lost
permission bits and are correctly rejected. The canary used the retained source
exports to build fresh temporary sealed/build copies, checked their bytes and
modes, and verified the historical sources and sealed evidence were unchanged.
It did not repair or relabel the old results.

## Qualification and experiment

The target's 46 native Contract tests and Core typecheck passed. The transport's
3 tests and the patched Core typecheck passed. Runner regression passed 666
tests before the final subject-publication assertion; 255 supplemental tests
passed including native materialization and that assertion. The full and supplemental
JUnit reports are under `/home/duozhou/run-artifacts/policy3108-*-tests-20260911.xml`.

The real compiled-server admission check passed at
`/home/duozhou/run-artifacts/policy3108-admission-20260911-v1`: network denial,
child-provider denial, reference execution, one restart, headless permissions,
and exact Contract issue/response reconciliation. It used dummy credentials and
an unreachable loopback upstream, not a real model call.

The authorized small pilot uses Luna Max, three concurrent ProContract tasks
(`fx`, `cheat`, `hush`), seed 42, 6 h / 1000 provider turns, and no monetary cap.
`runtime_protocol: policy-split-v1` is explicit. Do not enable
`PROGRAMBENCH_GROUNDED_REFERENCE`: this target retains its original Bash-based
black-box access and does not have the later typed reference tools. Candidate
containers remain network-none. The evaluator revision is pinned to the prior
pilot's `de0ddfb637590c7ecb54fa0b5301f6dc7dfbcee5`.

Archive sources, transport patch, binary, config, logs, usage, native evidence,
submissions, and all task evaluations under a fresh experiment root. Compare
raw scores and valid-delivery counts separately. The preceding grounded pilot's
raw mean was 78.4211%, with only 1/3 valid deliveries; its two invalid artifacts
are not a clean protocol-success comparator. This three-task development pilot
cannot establish general superiority or a kernel-only causal effect.
