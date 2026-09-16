# Final confirmation components

These host-side components prepare independent confirmation of a frozen solver-system revision. They do not run a solver, grant adoption, or replace the existing D/P comparisons. The intended final design uses the fixed official 200-task catalog, two new runs per arm and task, and a single candidate/baseline pair. Research D/P scores are not part of these 800 assignments.

## Preserve official score precision

`scripts.growth_final_analysis` accepts an externally verified scalar table. For official ProgramBench results, supply each valid score as its canonical integer counts:

```json
{
  "task_id": "an-official-instance-id",
  "arm": "candidate",
  "repeat": 1,
  "closed": true,
  "status": "valid",
  "score": {"resolved": 103, "total": 300}
}
```

The enclosing object has `schema_version: 1`, the externally registered `attempt_index`, all 200 `task_ids`, and `rows` covering both arms and repeats. Non-valid rows have `score: null`; retained counts from an invalid evaluation must not be used as a score. The analyst must not infer validity from the presence of counts.

The existing `growth_loop.measure` uses `Fraction(resolved, total)` after checking consistency with the verified official float. Final extraction must preserve those same counts. For example, baseline `1/3` and equal candidate groups scoring `103/300` and `109/300` have exactly a +2pp mean gain. Converting their binary floats to decimal text first yields a value slightly above +2pp. The count form avoids this false threshold crossing. JSON numbers remain supported for synthetic tables and legacy decimal inputs, where the module preserves their lexical precision; this does not recover counts already lost by a caller.

The numerical method is unchanged from the separately qualified prototype: average repeats within each task, then use 200 task clusters; require point gain strictly above `1/50` and a positive adjusted one-sided lower bound. Attempt `j` uses exact alpha `1 / (20*j*(j+1))`. Inference uses mpmath 1.4.1 at 80 digits with the existing numerical-boundary handling. Zero standard error is inconclusive. This is approximate task-cluster paired-t inference and does not establish exact infinite-horizon error control or generalization outside the fixed benchmark.

No means are returned while any original assignment is missing or open. After all assignments close, invalid scores prevent a numerical pass and remain explicit. A numeric pass has no formal acceptance or revision-switching authority.

Run the component with an already closed external table:

```sh
python -m scripts.growth_final_analysis --input closed-scalars.json --output analysis.json
```

It refuses to overwrite the output and binds the input and analyzer hashes in the result. The `research` dependency group pins the numerical dependency. Qualification tests use synthetic tasks, including exact-count boundary cases and the prototype's independent near-critical and tiny-variance counterexamples.

## Reuse the native execution and scoring boundary

A future final controller can use `ProgramBenchBackend.evaluate` with one assigned task per operation, retaining its native OpenCode execution, separate official scoring, cancellation and per-instance deadline behavior. Each arm/repeat needs a distinct output path and independently admitted workspace. No new provider or tool loop is needed.

`ProgramBenchBackend.validate_evaluation` rechecks a completed canonical result against configuration, runtime/policy identity, evaluator invocation, submission identity and official scoring. `independent_scores` recomputes the official full-map score and preserves invalid results and incomplete accounting. A final scalar extractor must call this trusted verification before retaining valid counts. The numerical analyzer cannot perform or substitute for that provenance check.

The existing `growth_compare.run` is not itself the final controller: it schedules full arms and has D/P screening semantics. Also, `growth_loop.operation` deliberately refuses an ambiguous started operation instead of silently rerunning solver work. Final integration must preserve that behavior and specify any evaluation-only recovery before admission.

## Work still required before an actual final attempt

Registration/schedule components are under separate implementation. A complete final execution path still needs the bound official catalog and evaluator inventory, qualified candidate and baseline, resource configuration, original six-hour deadline verification, a scheduler using the registered pair order, raw result retention with a full-cohort disclosure barrier, and a tested evaluation-only recovery path. All 800 assignments and all failed/cancelled attempts must remain in the external accounting.

No final attempt has been registered or started by this change. Existing running and frozen studies are unchanged. Final adoption still requires independent checks of execution, provenance and the stated inference assumptions in addition to the numerical result.
