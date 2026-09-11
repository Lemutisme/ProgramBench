# Grounded-evidence runner provenance

Status: opt-in implementation candidate, 2026-09-11. Pilot admission failures
are documented below; no performance improvement has been established.

## Baselines

- OpenCode: branch `grounded-evidence` at
  `17356a1a44a07e21a4ff096af42300ec6848123a` in
  `/home/duozhou/opencode-grounded-evidence`, directly based on the actual
  200-instance P0 `97d61022abdc33397ee97447c7bbc6f571574e1e`.
- Runner: branch `grounded-evidence` in
  `/home/duozhou/ProgramBench-grounded-evidence`, Git base
  `bd8226bdcf1c9ca888599afc2628da718e65dc8f` plus the frozen offline runner below.
- Inherited offline source archive:
  `/home/duozhou/run-artifacts/offline-sol-xhigh-200-20260910-v1/frozen/runner.tar.gz`.
- Archive SHA-256:
  `8e1f35ab2a95551c0eb6a348bdd6e4d15bd3605361e18aa5dfeff627380e40b2`.

The archive contains prior runner, offline gateway, evaluation, continuity, and
test fixes that were not in its Git base. Consequently the complete branch diff
must not be attributed to this enhancement. Old experiment configurations and
documents inherited from that archive describe their original coordinates, not
this new binary. Existing experiment directories are untouched.

## Changes beyond that archive

1. `campaign_materialize.build_view` makes a private build copy from the original
   materialization after verifying it matches the sealed evidence under its
   read-only permission mask. It preserves original permissions, including
   intentional read-only fixtures. ProContract and vanilla finalizers package
   this copy instead of the read-only evidence directory. Both original and
   sealed trees are checked for drift; neither is chmodded or deleted.
2. Cleanup of owned temporary trees fixes parent write permission and does not
   follow symlinks to chmod their targets. It is not a cleanup of old experiments.
3. Task guidance names `/candidate/reference` and `/workspace/executable`
   explicitly, separately from the candidate's `/candidate/executable`.
4. `PROGRAMBENCH_GROUNDED_REFERENCE=1` opts isolated ProContract jobs into a host
   reference binding. A disposable network-none container hashes the executable
   in the exact task image. The Contract ID, executable hash, fixed cwd, and
   candidate Location are retained in `workspace-state/reference-binding.json`.
   Identity changes are refused on retry. The issued authority includes
   `reference.run`, and the binding is passed to the new OpenCode binary.
   Official execute-only (`0111`) references are never made readable to the
   agent: the trusted image-hashing helper alone uses root/DAC_OVERRIDE. The
   candidate container still drops ALL capabilities. Before runtime release and
   restart, the host hashes the actual executable through Docker's archive API,
   validates root ownership/no write bits, and publishes a metadata/digest witness
   through the existing read-only channel mount. The tool verifies that witness.

Existing files changed relative to the archive are `campaign_freeze.py`,
`campaign_offline.py`, `campaign_procontract.py`, `campaign_runner.py`,
`campaign_vanilla_http.py`, `campaign_workspace.py`, and
`tests/test_campaign_runner.py`. New implementation/tests are
`campaign_materialize.py`, `test_build_materialization.py`, and
`test_reference_binding.py`, plus this documentation and the new profile.

## What the opt-in does not do

The reference tool records model-selected argv/stdin experiments; it does not
automatically approve them as settlement obligations. OpenCode's new
`Observation.check` API lets an issuer freeze complete reference receipts into
a Spec. This runner still uses its pre-existing, explicit `observations:` YAML
profiles for issuer-approved finite cases; those expected bytes are trusted
inputs, not automatically imported tool receipts. Default configuration does
not assert coverage that it has not checked.

The P0-derived binary does not include the later `contract_check` workflow or
full vanilla-disable adaptation. Only the ProContract arm is qualified by this
change. The required `vanilla` config entry exists for schema compatibility;
do not run it as a matched comparison. Dynamic evidence admission, stateful
observers, and a protected isolated evidence worker need separate work.

The container must remain network-none; the provider socket is not a general
proxy. Local shell execution inside that container is not itself a strong
isolation boundary between candidate code and same-user evidence storage.

## Validation

The final expanded regression passed 648 tests (zero failures), including
materialization, reference binding, native delivery, campaign recovery,
workspace isolation, and preflight boundaries. Its JUnit report is
`/home/duozhou/run-artifacts/grounded-evidence-runner-tests-20260911-v5.xml`.
The Core regression and design/trust limits are documented in
`/home/duozhou/opencode-grounded-evidence/specs/pro-contract-grounded-evidence.md`.

Use `scripts/opencode_programbench_grounded_luna.yaml` only with the new binary.
Its `arms.procontract.runtime_protocol: policy-split-v1` explicitly freezes the
wire protocol; new OpenCode commits must not rely on the historical commit
allowlist to infer whether Policy belongs outside Spec.
Before any model pilot, verify its frozen OpenCode revision and binary hash and
use a fresh output root. A no-model check is:

```bash
PROGRAMBENCH_GROUNDED_REFERENCE=1 \
OPENAI_API_KEY=no-model-admission-check \
OPENAI_BASE_URL=http://127.0.0.1:9/v1 \
uv run --frozen python -m scripts.check_offline_workspace \
  --config scripts/opencode_programbench_grounded_luna.yaml \
  --output /home/duozhou/run-artifacts/grounded-evidence-admission-NEW \
  --permission-probe --restart-checks 1
```

This deliberately uses a dummy credential and unreachable loopback upstream.
Do not add `--model-probe` or `--tool-probe` to a no-model check. It checks actual
network denial, child-gateway denial, server startup, headless permission, and
restart, not model behavior or benchmark score. Legacy `run_offline_sol.sh` and
`freeze_offline_experiment.py` have historical hardcoded roots; they are not
entrypoints for the new branch.

The real compiled-server check passed with output root
`/home/duozhou/run-artifacts/grounded-evidence-admission-20260911-v2`:
network denial, child-provider denial, reference execution, headless permissions,
and one stop/restart. The gateway database records zero admitted model requests.
Reference permissions remained `0111`. The first `v1` check exposed the
execute-only hashing issue; its artifacts are retained rather than overwritten.
The checked Linux x64 binary SHA-256 is
`1496785f285a01eb942e5f903a0e837a713e4324c60faed9c438337d52c7dd7b`.

## Pilot-discovered admission gaps

The first requested three-task pilot exposed additional admission gaps.
Experiment-local harness binary copies need mode `0555`, not
`0500`, because task images can use a different agent UID. This does not change
the reference's `0111` mode.

The next launch exposed two runner bugs: unknown source commits fell back to a
legacy Policy-in-Spec protocol, and `_expected_spec` reconstructed a fixed
three-capability authority, discarding the issued `reference.run` capability.
The new profile explicitly freezes its runtime protocol, and canonical issue
hashing/response validation retain the exact issued authority. These fixes do
not change the OpenCode binary, task budgets, or automatically admit new cases.
Failed roots `grounded-luna-pilot3-20260911-v1` and `v2` are retained. V1 had zero
provider requests. V2's fx and hush each sent one request that returned HTTP 200
before transport interruption; neither has a retained usage receipt. Their cost
is unknown, not zero, despite the runner's zero accounting before admission.

`check_offline_workspace --contract-probe` now validates real compiled-server
issue/response reconciliation as well as startup. It refuses real provider
credentials/upstreams: use the dummy/loopback environment in the command above.
The probe passed in `/home/duozhou/run-artifacts/grounded-contract-admission-20260911-v1`,
including the complete authority and external execution policy. This closes the
specific gap left by helper tests and a health-only admission smoke check.
The post-fix runner regression passed 653 tests; JUnit is retained at
`/home/duozhou/run-artifacts/grounded-protocol-tests-20260911.xml`.

After successful admission, explicitly authorize a small pilot before any new
full cohort. Preserve all artifacts and evaluate every selected task, including
failed deliveries. Keep the 6 h / 1000 provider-turn budget and no monetary cap;
do not introduce a forced minimum number of turns.
