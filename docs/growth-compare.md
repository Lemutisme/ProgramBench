# External full-revision comparison

`scripts.growth_compare` compares two already built, qualified AgentRevisions using the existing ProgramBench backend. It does not call a generator, manufacture a native handoff, change the solver policy, or establish an improved Improver. Results explicitly record `kind: external-code-intervention`, `native_generation_count: 0`, and `generator_executed: false`.

Use the existing version 2 study format, with these changes:

```json
{
  "version": 2,
  "mode": "external-comparison",
  "mutable_objects": ["agent_revision"],
  "initial": {"agent_revision": {"path": "/absolute/control/manifest.json", "sha256": "..."}},
  "comparison": {
    "kind": "external-code-intervention",
    "candidate": {"path": "/absolute/candidate/manifest.json", "sha256": "..."}
  },
  "criteria": {"min_mean_gain": 0.02, "missing": "reject"},
  "confirmation_criteria": {"min_mean_gain": 0.02, "missing": "reject"},
  "rounds": [{"id": "matched", "opportunities": 1, "seeds": [1], "confirmation_instances": ["independent-task"]}]
}
```

The remaining common study fields are required: configuration, exact initial binary, common revision recipe, frozen clean runner, evaluator wheelhouse, metadata, development instances, test-map choice, and generator image/context. The generator fields remain compatibility inputs and are frozen but never executed. There is exactly one external candidate and one preregistered D/P block. Both revisions must validate against the same recipe and contain identical solver policy bytes. Generator policy can differ because no generation is assigned.

```sh
PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_compare --study /absolute/study.json --output /absolute/execution --freeze-only
PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_compare --study /absolute/study.json --output /absolute/execution
```

The reusable API is `freeze_compare(study_path, root) -> (study, bindings)` and `run(study_path, root, backend=None)`. The default backend is `ProgramBenchBackend`; an external trusted owner can supply its existing bound backend. `growth_loop.run` rejects this mode so it cannot accidentally start native generation.

Before development evaluation, `frozen.json` binds both complete revisions, the controller sources and study bytes. `assignments.json` retains both arms, all D/P instances and repeats, deterministic operation order and specification hashes. Each operation uses the established `<block>/evaluation/<development|confirmation>/<incumbent|candidate-000>/<seed>` directory and evaluates that stage's instances through the existing campaign. `<block>/proposals-frozen.json` labels its subjects as externally qualified revisions. There is no `<block>/candidate-000/result.json` generation result.

The existing exact-rational `choose` calculation requires every assigned score to be valid and task-equal mean gain to be **strictly greater than 0.02** on D. Exactly +2pp is insufficient. Only then is P admitted, using the already frozen independent instances and both original revisions. P uses the same all-valid, strict-gain gate. No per-task no-regression requirement is added. `screen.json`, `confirmation-consumed.json`, `confirmation-decision.json` and `decision.json` are stored under the block. `completed.json` contains the recommendation and accounting; it does not switch an external official version pointer.

Every native instance retains its original six-hour deadline-only budget. Common configuration validation rejects cumulative provider-turn/action/money ceilings. Bounded individual operations, cancellation, offline scoring and native accounting remain with the existing backend. No new provider, tool or scoring loop is introduced.

`CANCEL` is checked before each backend admission and before P. The backend already observes it during inference/scoring and terminates its process group. A started operation without a sealed result cannot be silently retried after cancellation; it requires existing native/campaign recovery. Stopped attempts retain immutable `stopped/<content-hash>.json` snapshots. Snapshots and the final report list every assigned coordinate, including unstarted P, and preserve unknown cost/usage instead of filling zero. Their `started` count means coordinates assigned to an operation that started, not proof that each native job or provider request was admitted. Native and gateway usage are retained separately and are not added together.

Completed evaluations use `operation` caching; cached canonical results are revalidated through the backend's existing `validate_evaluation`, including missing/invalid-score reports. Cached operation failures must still match the retained failure record. Full revisions, recipe, configuration and controller identities are checked before admission and reuse. The comparison does not read hidden grader error details.

Qualification uses the repository's actual offline Docker toy-revision build and binary-execution fixture. Prescribed toy points test strict D/P gating, missing scores and rejection; they are not ProgramBench scores. Tests also cover unchanged-policy admission, configuration/result identity drift, missing artifacts, cancellation, ambiguous-work refusal and incomplete accounting. No model or official grader is invoked by this qualification.

Validation on 2026-09-16: 15 comparison tests passed against the final comparator source; 18 existing growth-loop/full-revision regressions passed with the unchanged shared `growth_loop` changes. Ruff and `git diff --check` passed. The toy Docker image was digest-pinned to `sha256:1421747c477203b974cb870829baf7a711bf64c7de2299c67952c294537359a3`.
