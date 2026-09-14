# ProContract capability policy experiments

The opt-in `fit` and `search` profiles test two distinct decisions in black-box
program reconstruction. They replace the additional execution policy; they do
not append to the historical `behavioral` or `adaptive` policy.

| `execution_policy` | Additional strategy |
| --- | --- |
| `behavioral` | Historical coverage and delivery guidance; unchanged default |
| `none` | No separate execution policy; the task brief and delivery terms still apply |
| `adaptive` | Historical representation and counterexample strategy |
| `fit` | Choose language, permitted libraries and runtime semantics suited to the task |
| `search` | Compare distinct executable alternatives, retain reference evidence and runnable checkpoints |

Use an explicit `runtime_protocol: policy-split-v1` with a compatible pinned
runtime. The new profiles reject runtimes that cannot bind their policy. For
example, the deadline-only runtime is
`57e89a94bf866b020c0bc3c3b73411f160136b67`.

```yaml
model: openai/gpt-5.6-luna
variant: max
limits:
  wall_time_seconds: 21600
  provider_turns: null
  procontract_actions: null
  total_cost_soft: null
  total_cost_hard: null
```

This is a fragment, not a complete campaign configuration. New campaigns must
also freeze both arm definitions, their binary/source identity, task images,
model catalog, evaluator revision and configuration. Existing frozen campaigns
must not be changed. Historical finite-budget configurations keep their meaning.

The runner admits the selected text as native `executionPolicy`. Contract goal,
brief, authority, evidence, deadline and delivery terms are identical across the
policy-split profiles for the same issue coordinates. Native SessionRunner owns
model execution; no additional Python inference loop is introduced. Restarting
with a different policy fails immutable issue reconciliation.

The frozen deadline-only binary exposes the standard bash tool, but does not
register the optional `reference_run`/`reference_read` tools. All new profiles
therefore share a corrected reference-access brief that describes the existing
`/candidate/reference` executable. Host reference attestation is retained. This
common correction is part of the new baseline and must be disclosed when
comparing with historical cohorts; it is not a `fit` or `search` treatment.

The gateway archives the exact first admitted provider request body in the
host-only `requests.db.initial_request` table, in the same transaction as its
request ledger row. The normal per-request hash and usage ledger remain. The
capture excludes HTTP headers and the upstream API credential and is not mounted
into the solver. It permits comparison of actual system text and tool schemas
between experimental profiles without retaining every expanding prompt body.

`search` currently guides alternative implementations within the native Session.
It does not add independent model workers, a tree-search engine, or a hidden-test
selector. Expected behavior must come from permitted reference execution or
explicit requirements. Inconclusive evidence is not a reason to erase an
alternative, and a few passing probes do not certify complete correctness.

These profiles are research hypotheses, not promoted defaults. A valid evaluation
requires same-model paired runs with repeated tasks, the same original six-hour
deadline and no cumulative call/action/cost caps. Report semantic scores separately
from delivery and infrastructure status. Any offline best-candidate score is a
diagnostic upper bound, not the score of the actual selection policy. Candidate
hidden scores and grader details must never be returned to an active solver.
