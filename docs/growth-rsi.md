# Shared-prefix policy RSI experiment

`scripts.growth_rsi` distinguishes a solver policy's adoption from the research
trial of a generator policy. Both are text artifacts produced through native
ProContract Sessions. The research controller schedules generation and independent
ProgramBench evaluation; it contains no second provider/tool loop.

The previous joint-adoption loop could only try a new generator after its paired
solver proposal passed both score gates. That solver's score is not a measurement
of the new generator's ability. This protocol instead tests the generator through
the performance of a subsequent proposal, with a matched fixed-generator control.

## One prefix, two descendants

1. A shared native generation uses initial solver pi0 and generator rho0. It
   proposes pi1 and rho1. A changed, valid native rho1 is sealed as an
   **unvalidated research successor before any score is observed**.
2. Incumbent and pi1 are evaluated on development D. A qualifying pi1 proceeds to
   confirmation P0. Pi changes only if both complete comparisons exceed the
   preregistered thresholds.
3. The completed prefix's development tool archive, D scores, rejected proposals,
   and shared incumbent are sealed as common input. One child executes rho0;
   another executes rho1. Completed native receipts bind the exact pi, rho and
   common feedback hash. P0 scores and traces are not returned as feedback.
4. Both child proposal operations close before any child D or P1 evaluation.
   One immutable barrier binds their results and policy identities. All valid
   children and the incumbent are measured on the same P1 regardless of the D
   screen. A failed proposal retains its missing comparison coordinates.
5. Solver adoption requires the **same child** to pass both D and P1. Among those
   eligible, the largest exact P1 gain wins, with predeclared order breaking ties.
   The exploratory recursive-minus-fixed comparison includes both assigned
   children, even when neither is adopted. It never promotes rho automatically.

This is three planned generation opportunities: one shared prefix and one child
per arm. Each arm has two opportunities when the prefix is included. Record
started operations, native admission receipts, completed native proposals and
failures separately. A changed, valid rho is necessary to start the fork. If none
is produced, record unused child opportunities and stop without a new attempt.
The shared prefix's work and costs are counted once.

P1 is a single preregistered comparison of frozen policies, not the old protocol's
single-finalist P block. No candidate may be regenerated or replaced after seeing
P1. A changed incumbent may reveal whether P0 caused adoption; this limited
adaptive exposure should be disclosed. Task identifiers being new to this lineage
does not establish absence of historical research exposure.

## Frozen schema and execution

The ordinary version-1 growth study supplies runtime, model, images, offline
evaluator dependencies, initial policy/context hashes, D/P assignments and score
criteria. Additionally it must declare:

```json
{
  "mode": "recursive",
  "mutable_objects": ["solver_policy", "generator_policy"],
  "rsi": {
    "protocol": "shared-prefix-fork-v1",
    "rho_succession": "changed-native-successor-independent-of-pi",
    "confirmation": "all-frozen-children",
    "no_successor": "stop-without-retry"
  },
  "feedback": {
    "protocol": "native-tool-feedback-v1",
    "sources": [],
    "inline_bytes": 32768
  },
  "rounds": [
    {"id": "generation-0", "opportunities": 1, "seeds": [17], "confirmation_instances": ["p0"]},
    {"id": "generation-1", "opportunities": 2, "seeds": [17], "confirmation_instances": ["p1"]}
  ]
}
```

Use actual frozen instance identifiers in a real study. `feedback.sources` may
reference existing closed D archives with explicit hashes; subsequent D feedback
is prepared automatically. `inline_bytes` bounds context transport, not inference
or available archive recall. Complete retained tool records remain available to
the generator. Solvers receive their own task and policy, not research archives.

```sh
PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_rsi \
  --study /absolute/study.json --output /absolute/execution --freeze-only

PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_rsi \
  --study /absolute/study.json --output /absolute/execution
```

The original six-hour deadline is the only cumulative instance budget. Existing
isolation, cancellation, individual operation bounds, infrastructure guards and
known/unknown usage accounting remain in force. An existing operation with no
closed result is ambiguous and is never automatically reissued. Cached native
proposals revalidate their bindings and artifacts; cached valid evaluations
revalidate their original scoring provenance without running another grader.

## Interpretation and qualification

Local unit tests cover separate pi/rho transitions, failed/missing coordinates,
the two-child barrier, D/P joint eligibility, cancellation and exact retry. The
separate `scripts.check_growth_rsi_native` qualification executes real native
Contracts with a scripted provider and independently executed toy programs. Its
toy scores are control-path evidence, not ProgramBench performance.

One shared fork is an exploratory test. Repeated independent prefixes and fresh
task blocks are needed for a reliable generator-quality claim. This implementation
only evolves solver/generator policy text; automatic harness/tool code evolution
is not connected. No global default or paper performance baseline changes merely
because the recursive path executes.
