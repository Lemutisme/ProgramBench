# Executable AgentRevision research

Version 2 of `scripts.growth_loop` accepts a whole, externally rebuilt Agent
revision. A candidate can change source compiled into the native runtime,
including tools, context, search behavior and improver behavior, together with
the solver and generator policies. Generation and task execution still use the
native Session/Contract/provider/tool path. Python schedules research operations
and verifies their receipts; it contains no second model/tool loop.

This is an executable-revision research path, not evidence of higher
ProgramBench scores or a better improver. The version-1 policy-only path and
`growth_rsi` shared-prefix policy experiment remain supported. Their historical
artifacts are not migrated. This release does not automatically evolve the
external research controller, grader, state migrations or Kernel succession.

## One revision, one acceptance subject

A native generation hands off exactly four bounded artifacts:

- `agent-revision.json`: the exact parent SHA256, rationale and change metadata;
- `source.patch`: a UTF-8 Git patch relative to the supplied parent source;
- `solver-policy.txt` and `generator-policy.txt`: nonempty UTF-8 policy artifacts.

Example proposal metadata:

```json
{
  "parent_sha256": "<assigned AgentRevision manifest SHA256>",
  "rationale": "Repeated failed tool inputs leave an unproductive continuation.",
  "metadata": {
    "expected_effect": "Restore useful execution after an observed stream failure.",
    "failure_modes": ["Interrupting a healthy stream", "Losing a completed sibling tool"],
    "permission_changes": [],
    "state_migration": null,
    "recovery": "Keep existing tasks on their original revision; route new tasks to the parent if rejected."
  }
}
```

These fields are hypotheses and requests. They do not authorize a change or
certify its effect. Host classification also examines the actual changed paths;
candidate labels cannot bypass it.

After native replay, export and handoff attestation, the issuer reconstructs the
parent source plus patch. It freezes the complete source archive and per-file
identity, new clean Git commit, patch and parent ancestry, exact policy bytes,
dependency/build recipe identity, executable bytes, and independent build/check
receipts into one manifest. The manifest has `adoption_authority: false`.
The handoff-to-build record binds that manifest to its native Contract, Session,
parent and exported subject. The manifest SHA256 is the revision identifier.

The generator receives a copied, independently verified source tree with a new
local Git index. Its source view includes tracked files ignored by `.gitignore`
and safe relative symlinks. It receives neither host Git metadata nor the
issuer's study, score or adoption files as writable mounts. Archived revision
feedback strips host paths and build/check details.

## External build and execution boundary

`scripts.agent_revision.freeze_initial` builds the initial revision from an exact
source commit. `build_revision` rebuilds each proposed child. The externally
frozen recipe selects the image, dependency files, build argv, output path and
independent qualification scripts. A candidate cannot replace this recipe.

```python
initial = freeze_initial(
    absolute_output,
    {"repo": "/absolute/clean-source", "commit": "<full commit>"},
    solver_policy_ref,
    generator_policy_ref,
    recipe,
)
```

References contain exactly `path` and `sha256`, with canonical absolute regular
file paths. Recipe shape:

```json
{
  "version": 1,
  "image": "sha256:<local immutable image ID>",
  "build": {"argv": ["/bin/sh", "-c", "<trusted offline build command>"], "timeout_seconds": 1800},
  "qualifications": [{
    "name": "native-protocol",
    "argv": ["python3", "/trusted/check"],
    "timeout_seconds": 300,
    "script": {"path": "/absolute/trusted-check.py", "sha256": "<SHA256>"}
  }],
  "dependencies": {"inputs.tar.gz": {"path": "/absolute/inputs.tar.gz", "sha256": "<SHA256>"}},
  "output": "opencode"
}
```

Build and qualification run in separate Docker containers with no network,
credentials, Docker socket or writable host mounts. Source and dependencies are
read-only; build scratch and output use bounded tmpfs. The host streams only the
specified regular executable as bounded data and qualifies those frozen bytes.
Timeout, output overflow, failed checks and cancellation retain evidence and
prevent qualification. Ambiguous builds are not silently rerun.

The existing installer previously ran supplied executables on the host to query
`--version` and `models`. New revision configurations require digest-pinned
`binary_probe_image`; installation and reuse execute these probes inside Docker.
An immutable installation-isolation record rejects changing or dropping this
boundary on reuse. Version-1 configurations retain their previous behavior.

Cache reuse checks source HEAD, clean tracked and ignored state, archive bytes,
parent/patch ancestry, binary, dependencies, scripts and all bound receipts.
Safe baseline relative symlinks are preserved; escaping links, submodules,
special files and candidate link changes are rejected.

## Freeze and execute a version-2 study

Use the ordinary growth study fields for evaluator wheelhouse, official task
metadata, runner, model, D/P assignments, generator image and context. Change:

```json
{
  "version": 2,
  "mode": "recursive",
  "mutable_objects": ["agent_revision"],
  "initial": {"agent_revision": {"path": "/absolute/initial/manifest.json", "sha256": "<SHA256>"}},
  "revision_recipe": "<the recipe object above>",
  "criteria": {"min_mean_gain": 0.02, "missing": "reject"},
  "confirmation_criteria": {"min_mean_gain": 0.02, "missing": "reject"}
}
```

`revision_recipe` is an object, not a path or candidate artifact. Obtain the
initial runner configuration with `agent_revision.runtime_config`. The frozen
evaluation runner must include the new isolated installation implementation.
`runtime_sha256` identifies the initial binary; each evaluation specification
derives its own binary SHA256 from its selected revision.

```sh
PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_loop \
  --study /absolute/study.json --output /absolute/new-study --freeze-only

PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_loop \
  --study /absolute/study.json --output /absolute/new-study
```

All proposals in a round close and are sealed before any round evaluation.
Candidate and incumbent use identical frozen model, six-hour deadline-only
limits, gateway, task assignment, grader and evaluation dependencies. Only the
Agent revision changes. No cumulative provider, tool, request or monetary cap is
introduced. Build resource/log guards bound individual infrastructure operations.
An existing native task always retains its original deadline and runtime.

An optional frozen `infrastructure_guard.minimum_disk_free_bytes` pauses each
new generator/container or solver-job admission below the host disk floor.
Active jobs keep running with their original deadlines, and explicit cancellation
still works while admission is paused. This is a storage protection rule, not a
cumulative model resource budget.

Development selection uses exact task-equal mean score. Missing or invalid
assigned measurements prevent adoption. A qualifying finalist must also pass
the frozen fresh-confirmation mean threshold. Version 2 has no per-task
non-regression criterion; task deltas remain visible for diagnosis. Neither
intermediate regressions nor rejected candidates erase earlier observations.

Adoption updates the study's complete revision binding. The next generation's
native generator actually installs that revision's runtime and generator policy;
its solver evaluations use the same revision's solver policy and runtime.
Cached results revalidate identities without starting another model or grader.
The global/default version pointer is not changed by this research loop.

## Limits of the current acceptance path

Sensitive Kernel, permission and migration paths, or explicit requests for
authority/state changes, are archived as `proposal-only`. They do not execute
or stop the remainder of the study. Automatic succession needs independently
tested event replay, crash/retry/concurrency/authority invalidation, migration
and recovery checks that are not implemented here. Path classification is a
conservative screening rule, not a proof of semantic equivalence.

Independent scripts must test the intended qualification properties. A smoke
check is not a deadline/gateway/permission qualification. Code identity and
passing local checks are not a performance claim. Before a new paid cohort,
qualify its actual source/dependencies/binary/image across native admission,
continuation beyond former count limits, original-deadline termination,
gateway/credential isolation, cancellation, accounting and official scoring.

Study adoption measures solver quality. Because the runtime can also implement
improver behavior, using a successor demonstrates recursive participation, but
not improved improvement ability. A system-level matched shared-prefix fork,
with descendant quality on independent tasks and complete research costs,
remains necessary. `growth_rsi` currently implements that comparison for policy
artifacts only.

## Qualification evidence

The tests execute actual offline parent/child builds and independent checks,
rejection and tampering cases, full-revision selection and subsequent use, and
isolated installation/reuse. The controller test uses deliberately prescribed
mutations and tiny executable output expectations; its scores are synthetic.

`scripts.check_agent_revision_native` runs a source-import and typed-artifact
handoff through a real previously trusted native runtime with a scripted
provider. Its synthetic parent revision binds that trusted binary as an explicit
dependency. Actual Docker import places relative symlinks, tracked ignored files
and a nested Git index in the same generation directory used by native replay,
ready reporting, export and principal attestation. It also checks the original
deadline, denied cross-UID control writes, cached receipt substitution and
cleanup. It does not measure a newly generated runtime or invoke the official
ProgramBench grader.
