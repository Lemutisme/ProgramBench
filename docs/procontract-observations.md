# Issuer-owned CLI observations

Status: opt-in development feature on `evidence-continuity`, 2026-09-07. No benchmark improvement has been measured for this feature.

The native ProContract adapter can require finite approved CLI observations in addition to the existing compile and `validate.sh` checks. These are independently specified expectations, not tests loaded from the candidate's mutable files.

## Configuration

Add an optional top-level field to the usual campaign YAML:

```yaml
observations:
  your-selected-instance-id:
    - id: version
      args: ["--version"]
      stdin: ""
      stdout: "1.0\n"
      stderr: ""
      exit: 0
```

Use actual selected instance IDs and issuer-approved expected bytes, not the illustrative value above. Do not derive profiles from hidden benchmark tests. Every supplied ID must be selected; typos and unused profiles fail before installation. Each profile contains 1–32 cases with unique IDs. Stdin, stdout, and stderr are UTF-8 strings; exit is an integer from 0 to 255. Each case receives a fixed 2-second timeout. The compiler validates size bounds before execution.

Omitting `observations` leaves the existing configuration serialization and native delivery policy unchanged by this feature. The field applies only to the ProContract arm. `semantic-v0` cannot accept it. Native policy-split, policy-none, adaptive, and current protocol paths support it without changing the execution policy or shared budgets.

## Admission and recovery

The campaign runner admits `procontract-observations.json` in the host job root before entering the native adapter. The file records the exact cases, timeout, format version, and `stdio-v1` observer. Publication is no-clobber and durable. Exact retries succeed; changed or removed cases fail rather than silently weakening an already issued claim.

The adapter compiles the cases into an additional native replay check. Expected values reside in the issued argv, not `validate.sh`. Issue and replay policy hashes include this check; the finite-case claim is bound to the same exact subject as the other replay checks. Restart reconstructs the same issue, including canonical native JSON field order. A missing admitted file or a result for the old weaker replay policy cannot substitute for it.

The check executes `./executable` from the replay working directory and compares exact output and exit. It kills the candidate's process group on completion or timeout, retains bounded diagnostics, and puts the first counterexample before the case summary. It does not require every intermediate candidate to pass.

To change cases or observation semantics, freeze a new experiment/job coordinate. Live profile revision, counterexample admission during search, and evidence retirement are not implemented here.

## Trust and scope

This is an authority/retention mechanism for a small initial case set, not a completeness proof or automatic reference recorder. Expected values are trusted issuer inputs. Python isolated mode avoids candidate-local import shadowing but is not an adversarial sandbox. Interpreter integrity, native replay, environment validity, and host isolation remain assumptions.

`stdio-v1` does not interpret PTY screens, compare socket protocols, reset stateful services, or validate arbitrary binary formats. A longer-lived daemon or rendered terminal behavior needs a different explicitly approved observer. Passing these cases does not prove universal correctness or preserve hidden-test scores.

## Reproducibility

The historical full-cohort P0 ProgramBench branch is `procontract-p0` at `8c156d4475c4eb1fc006123cbb9f45b72dccef01`; its paired OpenCode binary came from `97d61022abdc33397ee97447c7bbc6f571574e1e`. Do not overwrite that campaign.

This development worktree starts at `bd8226bdcf1c9ca888599afc2628da718e65dc8f` and retains prior uncommitted runner/evaluator fixes, plus the execution-replacement continuity changes from `ccffe445c7a8272f9598b4fc136a358dadd601d9`. Freeze all source changes and the built binary before an actual campaign: branch HEAD alone does not identify the uncommitted implementation. For an ablation, hold binary, runner, model, task access, and non-monetary budgets fixed while changing only observation admission. Count verification costs and rejected deliveries, not just successful scores.

Implementation tests are in `tests/test_campaign_observations.py`, `tests/test_campaign_procontract.py`, `tests/test_run_opencode.py`, and `tests/test_campaign_runner.py`. The corresponding OpenCode design and baseline provenance are documented in `/home/duozhou/opencode-evidence-continuity/specs/pro-contract-evidence-continuity.md`.
