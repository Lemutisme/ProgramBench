# Private owner for a registered final confirmation

`scripts.growth_final_owner` composes the existing final native input binding,
execution, collection and registry APIs with the retained qualified Linux owner.
It does not allocate attempts, select candidates, initialize registries, implement
a provider loop, change budgets or grant adoption. The source template is
`scripts/growth_final_owner_launch.py`, copied from the previously qualified
`procontract-policy-transfer-20260917/matched-1/launch.py` artifact.

The template's SHA256 is
`865fbe5ee7950860c7af5ccb69ed14922acdeae7c2f1a6e6655effaf071bf8cd`.
Preparation derives the actual `launch.py` by changing only the `PYTHON` string
assignment to the invocation path verified by `FinalNativeInputs`. This is an
interpreter-coordinate adaptation, not byte identity of the resulting launcher.
The template and actual launcher have separate frozen references and separate
root-review hashes. A test normalizes only that assignment and compares the
entire remaining AST. Nonce admission, single-launch locks, PID/start-time/boot
identity, Linux subreaper ownership, pidfd signalling, bounded descendant cleanup
and the launcher's environment allowlist retain their original source.

## Preparation and exact review

The external owner provides an already registered attempt, its existing registry
reference and committed receipt, an independently designated official catalog
hash and a hash-bound ripgrep executable. Before preparation, the owner root and
its existing `execution` subdirectory must be canonical directories owned by the
current UID with mode `0700`. The registration's native-inputs manifest points to
that same execution directory. Call from the actual frozen controller and its
qualified interpreter:

```python
from scripts.growth_final_owner import prepare

receipt = prepare(
    owner_root,
    registration=registration_reference,
    registry_ref=existing_registry_reference,
    registry_receipt=committed_registration_receipt,
    expected_catalog_sha256=designated_official_catalog_sha256,
    ripgrep=ripgrep_reference,
)
```

Preparation constructs `FinalNativeInputs`, recursively checks the actual native
resources and both revisions, verifies the active registry receipt, and derives
all 800 specifications and fixed `slots/000001` through `slots/000800` outputs.
Each bulk derivation revalidates both revisions once, avoiding 800 repeated
hashes of the same runtime. Every native operation still uses strict individual
qualification before admission and result validation.
It freezes this inventory in `execution/execution.json` before any launch. It
also freezes the actual `launch.py`, `control.py`, `controller.py`, `entrypoint.py`,
owner configuration/resources and complete input manifest. The wrappers verify
the owner's source hash and import location. The inventory binds actual public
and controller argv, the interpreter invocation path and resolved executable
bytes, source and dependency-coordinate files, revisions, native study and
resources. The 20 GiB disk guard is required; native qualification retains the
original six-hour deadline and absent cumulative limits.

`OWNER_PREPARATION.json` reports `prepared-not-authorized`. Neither that file nor
a registry receipt authorizes launch. A separate `ROOT_LAUNCH_REVIEW.json` must
contain the following exact values after independent review:

```json
{
  "decision": "launch_registered_final_once",
  "frozen_manifest_sha256": "<OWNER_PREPARATION.manifest.sha256>",
  "launcher_template_sha256": "<OWNER_PREPARATION.launcher_template_sha256>",
  "launcher_sha256": "<OWNER_PREPARATION.launcher_sha256>",
  "execution_sha256": "<OWNER_PREPARATION.execution.sha256>",
  "registry_identity": "<the existing external registry identity>",
  "attempt_index": 1
}
```

The example index is only a placeholder; use the committed receipt's original
index. The reviewer must review the actual immutable files and references, not
copy a receipt from another root. `check(root)` rechecks these identities, the
template adaptation and all native specifications. Nothing in the controller
generates a new schedule, registration or output coordinate after admission.

## Execution, cancellation and disclosure

Use the frozen `public_entrypoint` argv from `OWNER_CONFIG.json`. No option checks
the reviewed owner without launching; `--start` requests one retained supervisor,
`--cancel` stops admission, `--status` returns states/counts, and `--collect`
invokes collection only. A duplicate start exits nonzero without cancelling the
already requested launch. A failed check-only invocation records a private check
diagnostic without changing the registered attempt. Launch errors and controller
failures remain nonzero and retain private diagnostics.

The controller verifies that its direct parent is the live recorded supervisor,
including start ticks, process group and boot identity. It supplies the actual
native backend and constructor-bound qualifier to `growth_final_execution.run`.
Closed execution passes to `growth_final_collect.collect`; only a fully
revalidated collection creates the completion marker. The controller exits zero
only when owner readout is `closed`; cancellation, failure, halted or incomplete
execution exits nonzero with its original records and `CANCEL` retained. A separate `--collect`
never calls `run`, reissues solver work or resets deadlines. Collection-only
recovery does not turn an ambiguous started operation into a new inference run.

Every owner cancellation/failure first exposes the original `execution/CANCEL`,
then appends the corresponding state to the same external registry allocation.
Repeated cancellation does not append duplicate state events or refund an index.
The retained launcher's cancellation, supervisor-failure and cleanup-failure
callbacks use this same hook. If the ledger is missing, corrupt or unavailable,
the error propagates, `OWNER_LEDGER_FAILURE.json` remains and `CANCEL` is retained.
There is no fallback registry initialization or automatic restart.

Raw native outputs, scores, collection scalars and accounting remain below the
private owner root. Public readout never opens a partial result: it only counts
started/result file presence and returns states/counts. All 800 result files
without collection mean `awaiting-collection`; `closed` requires the completion
marker's exact execution identity and all 800 original result files. Redirected
slot paths and mismatched completion markers are rejected. Cancellation/failure
states remain visible even if a later collection closes available evidence.

The owner UID and privileged host processes are trusted. Mode `0700` protects
against other ordinary UIDs; it does not stop another process with the same UID
from reading or changing files. Root review must separately establish the real
solver/researcher filesystem and credential boundary. This module is not a
same-UID security sandbox or protection from a privileged owner rolling back the
external ledger.

## Qualification scope

The owner tests use temporary synthetic registries, synthetic 800-slot results,
and independently copied miniature qualified revisions/native inputs. They
exercise real final execution and collection with a synthetic backend, real
retained-launcher cancellation/nonce rejection, exact interpreter materialization,
root review, private paths, failed ledger handling and direct controller rejection
before native admission. No test runs a model, official grader, formal final
registration or native evaluation operation. Synthetic result presence is not a
performance result.

The original fixed interpreter `/home/duozhou/ProgramBench/.venv/bin/python`
lacked `mpmath`; actual native preparation failed before admission. That failure
is retained independently. Qualification uses
`/home/duozhou/ProgramBench-final-native/.venv/bin/python` with `mpmath 1.4.1`,
without changing the shared environment. The adapted launcher's algorithm is
unchanged; this targeted suite does not claim a fresh full supervisor lifecycle
qualification. Production admission still requires the externally designated
official resources, actual frozen revisions and registry, exact root review,
real composed deadline/lifecycle verification, storage isolation and the
separately qualified evaluation-only recovery path described in
`final-confirmation.md`.
