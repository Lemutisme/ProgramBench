# Exact policy artifacts

The campaign runner can evaluate an externally generated execution policy without
editing the runner or OpenCode. Built-in policies keep their existing behavior.

```yaml
arms:
  procontract:
    # Existing repo, revision and binary coordinates are still required.
    mode: procontract
    runtime_protocol: policy-split-v1
    execution_policy: artifact
    policy_file: ./policies/candidate.txt
    policy_sha256: <SHA256 of the exact UTF-8 file bytes>
```

The parser reads the file once, checks its hash, and retains its exact bytes in an
immutable policy value. Both campaign manifest paths retain the hash and base64
bytes. Reloading changed source bytes fails; supplying a new matching hash still
cannot resume an existing campaign with different policy content. Files must be
nonempty UTF-8 without NUL, with a 64 KiB bound on the individual configuration
read. CRLF and Unicode are preserved without normalization.

The exact text is passed to native `executionPolicy`. It does not enter Contract
terms, authority, evidence, or replay rules. Existing native binding validation
and durable issue reconciliation detect substitution. Execution remains owned
by the native Session runner, including continuation and cancellation. Instance
budgets and network isolation do not change. The offline qualification script
also carries the configured deadline-only setting into its workspace request.

An artifact is a proposed strategy, not an approved improvement. This interface
does not generate candidates, choose a winner, authorize adoption, or establish
recursive self-improvement. A separate experimental controller must preserve all
assigned results, verify independent performance evidence, and bind an adopted
artifact to both task execution and subsequent candidate generation before
claiming recursive participation. Hash equality establishes identity, not the
origin or quality of the artifact.

Validation commands:

```bash
python -m pytest -q tests/test_campaign_policy.py tests/test_run_opencode.py \
  tests/test_campaign_procontract.py tests/test_campaign_runner.py
```
