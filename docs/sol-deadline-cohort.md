# Sol xhigh deadline-only cohort

This worktree ports deadline-only runner support onto the reviewed baseline
`985ad9e`, whose tracked source matches the frozen Luna reliability runner.
The only intended model-work changes are the absence of cumulative turn/action/
wire caps and enforcement of the existing absolute six-hour deadline, including
gateway restarts and in-flight transport. Existing finite budgets remain valid.

The native quality treatment is not enabled. Grounded/typed reference additions
are not enabled, and no search/stopping policy is changed. Unknown usage remains
explicit; complete raw evidence and historical campaigns are preserved.

The paired runtime is `/home/duozhou/opencode-deadline-only`. Validation,
frozen source and binary coordinates, model access probe, qualification and the
fresh full campaign live under:

`/home/duozhou/run-artifacts/programbench-sol-xhigh-6h-20260914`

The requested model is exactly `openai/gpt-5.6-sol`, variant `xhigh`, not the
family alias. Candidate inference remains network-none with a peer-pinned
host-only provider gateway. No cost ceiling is imposed. Full inference uses
8 workers and is followed by independent evaluation. This is a new `6h-only`
profile, not a strictly budget-matched rerun of Luna/Terra's `6h/1000-turn` profile.

Known residuals from the prior cohorts (large verification/evaluation records,
unrun test branches, and snapshot ownership) are not claimed fixed by this port.
