# Native ProgramBench deadline-only execution

New development cohorts use `6h-only`: each instance receives one absolute six-hour deadline when its native Contract issue record is first durably published. A completed task may finish earlier. Historical cohorts and binaries are unchanged.

```yaml
limits:
  wall_time_seconds: 21600
  provider_turns: null
  procontract_actions: null
  total_cost_soft: null
  total_cost_hard: null
```

Use `--arm procontract`. The legacy vanilla Session adapter still has finite-turn execution semantics and is rejected at planning for this profile. Controlled quality comparisons run two native cohorts with the same model, binary, policy, task selection, and deadline profile; the quality mechanism is the intervention.

The YAML null values become absent `turns` and `actions` properties in the native Contract budget. They are not large finite limits. Native Schema/Core/Session must support these omitted fields; this runner must not be paired with a historical runtime.

The provider gateway receives the trusted `procontract-issue.json` path. Before accepting its first model request, it reads the exact Contract deadline, verifies six-hour coordinates, and binds the entire issue file hash plus deadline in its durable SQLite ledger. Missing or replaced issue records fail closed. The gateway refuses new work at that deadline and interrupts in-flight upstream and downstream sockets. Restarting the gateway or changing Sessions does not regrant time. Credentials stay on the trusted host and candidate network access remains disabled.

Deadline-only gateways have no cumulative accepted-request or wire-attempt ceiling. The native adapter and circuit breaker accept larger turn/action counts without labelling them as terminal overshoot. Historical finite gateways and breaker snapshots retain their existing limits. API payload byte bounds, timeouts, explicit cancellation, and infrastructure circuit breakers remain in place.

All counters remain recorded. Projected histories are not rejected because they exceed the old 2,001-message limit. Missing usage/cost is marked per turn and survives breaker serialization and result publication. `cost` is the sum of known costs; when `usage_incomplete` is true it must not be treated as the complete cost. Gateway records preserve missing upstream usage as null.

## Verification

Run from the runner repository with the existing environment, for example:

```sh
PYTHONPATH=src:. /home/duozhou/ProgramBench-cohort-repair/.venv/bin/python -m pytest tests/test_deadline_only.py -q
```

The deterministic tests record more than 3,000 admissions through production gateway methods and SQLite, reopen that durable ledger, verify original-deadline enforcement and refusal of regrants, interrupt a real local HTTP stream at the deadline, round-trip a 3,001-turn history with unknown cost, and retain explicit historical finite-budget enforcement. The admission loop does not make 3,000 HTTP round trips. No test calls a paid model.

Before paid experiments, combine this runner verification with the matching native runtime's admission/reservation/Session continuation tests and a frozen-binary smoke run. These checks establish budget semantics; they do not establish a ProgramBench score improvement.
