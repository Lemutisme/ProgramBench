# Workspace startup resource ownership

Offline workspace admission starts a trusted provider gateway before container ownership checks and cleanroom preparation. Previously, a failure before the `DockerWorkspace` handoff left that gateway running: normal cleanup only knew about a successfully constructed workspace.

`WorkspaceLease` now owns the directly spawned gateway process during startup. On admission failure, leaving the lease terminates and waits for that child, escalating to a kill after three seconds and allowing three more seconds to reap it. Cleanup uses the original `Popen` handle and checks the lease's original process/thread owner; a changed `gateway.json` cannot redirect the signal. Existing gateways returned by `ensure_gateway` are borrowed and are not added to the new-process cleanup set.

Only a successful `ensure_workspace` transfers gateway lifetime to the existing durable workspace cleanup path. Startup cleanup neither adopts nor removes containers and does not remove or rewrite the request ledger. All existing Contract deadlines, policies, admission counts, and historical artifacts retain their prior semantics.

Pre-provider infrastructure failures now enter the existing persistent system breaker under `workspace_startup`. The threshold is unchanged: three distinct jobs in the existing failure window pause further admission. The scheduler returns this persistent pause immediately rather than waiting for an unrelated provider backoff first. Explicit system acknowledgement remains required.

Regression tests exercise actual gateway subprocesses and local denied requests with zero upstream calls. Fault injection covers foreign-container rejection, cleanroom-copy failure, a stopped gateway that requires forced termination, and a replaced PID file pointing to an unrelated live process. They verify that only the new child exits and that the ledger survives. A scheduler test verifies that the fourth task remains unadmitted after three distinct pre-provider failures.
