"""Registered paired native dispatch, with no score-bearing progress interface.

The caller supplies a trusted recursive qualification boundary and an already
registered, frozen native specification for every slot. Registry envelopes do
not supply that boundary or authorize a real launch. This module has no CLI,
provider loop, grader implementation, adoption decision, or count budget.

Arm order is host submission order. Each active block owns two worker positions
until both arms finish; provider/token/tool ordering is deliberately unspecified.
Cancellation uses the backend's actual shared root/CANCEL and drains all owned
workers before releasing the cohort lock. An interrupted operation is not retried.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import threading
import uuid

from scripts.growth_final_registry import _database, _json, _keys, _path, _reference, validate_registration
from scripts.growth_loop import GrowthError, GrowthInfrastructureError, encode, operation, read, record, seal, sha256


KIND = "final-evaluation"


def validate_execution(execution, *, registry_ref, expected_catalog_sha256, require_active=False):
    """Read-only ledger/input validation; return the registered coordinates.

Collectors may audit a cancelled attempt. Admission additionally requires the
absence of durable cancelled/failed events in the externally owned registry.
The mandatory run/collector qualifier must recursively verify native inputs.
"""
    _keys(execution, {"schema_version", "registration", "registry_receipt", "slots"}, "execution")
    if type(execution["schema_version"]) is not int or execution["schema_version"] != 1:
        raise GrowthError("unsupported final execution schema")
    registration = validate_registration(_json(_reference(execution["registration"], verify=True)),
                                         expected_catalog_sha256=expected_catalog_sha256)
    with _database(registry_ref) as connection:
        rows = connection.execute("SELECT j,receipt FROM attempt WHERE registration_sha256=?",
                                  (execution["registration"]["sha256"],)).fetchall()
        if len(rows) != 1 or rows[0][1] != encode(execution["registry_receipt"]):
            raise GrowthError("execution is not bound to this registry's committed allocation")
        if require_active and connection.execute("SELECT 1 FROM event WHERE attempt_index=?", (rows[0][0],)).fetchone():
            raise GrowthError("registered attempt was cancelled or failed; fresh admission is forbidden")
    if not isinstance(execution["slots"], list) or len(execution["slots"]) != 800:
        raise GrowthError("execution requires all 800 registered slots")
    for entry, slot in zip(execution["slots"], registration["schedule"]):
        _keys(entry, {"slot", "output", "specification"}, "execution slot")
        if encode(entry["slot"]) != encode(slot) or entry["output"] != f"slots/{slot['slot']:06d}":
            raise GrowthError("execution slot/order/output differs from its registered coordinate")
        specification = entry["specification"]
        if (not isinstance(specification, dict) or specification.get("instances") != [slot["task_id"]]
                or encode(specification.get("agent_revision")) != encode(registration["revisions"][slot["arm"]])):
            raise GrowthError("native specification must bind the exact task and registered arm revision")
    return registration


def slot_identity(execution, entry, *, execution_sha256=None):
    return {"schema_version": 1, "execution_sha256": execution_sha256 or sha256(encode(execution)), "slot": entry["slot"],
            "output": entry["output"], "specification_sha256": sha256(encode(entry["specification"]))}


def validate_slot_binding(root, execution, entry, *, require_started=True, execution_sha256=None):
    """Validate operation ownership without reading result/score bytes."""
    output = _output(root, entry)
    if _file(output / "execution-error.json"):
        raise GrowthError("previously rejected execution evidence cannot become a valid slot")
    if (not _file(output / "slot.json") or encode(read(output / "slot.json"))
            != encode(slot_identity(execution, entry, execution_sha256=execution_sha256))):
        raise GrowthError("slot invocation identity changed or is missing")
    if not _file(output / "specification.json") or encode(read(output / "specification.json")) != encode(entry["specification"]):
        raise GrowthError("operation specification changed or is missing")
    if require_started:
        if not _file(output / "started.json"):
            raise GrowthError("result has no native operation admission receipt")
        started = read(output / "started.json")
        _keys(started, {"kind", "time", "specification_sha256"}, "operation admission")
        if (started["kind"] != KIND or type(started["time"]) not in {int, float}
                or not math.isfinite(started["time"]) or started["time"] <= 0
                or started["specification_sha256"] != sha256(encode(entry["specification"]))):
            raise GrowthError("operation admission does not bind the frozen specification")
    return output


def validate_failure(output, result):
    """Recognize only growth_loop.operation's canonical exception receipt.

    This is closed-invalid, never an official zero, and never recovered into a
    valid native evaluation. A native report with status=failed but no exception
    receipt still requires backend.validate_evaluation.
    """
    if not _file(output / "failure.json"):
        return False
    required = {"status", "error_type", "known_cost", "usage_incomplete", "details"}
    optional = {"failure_classification", "retained_usage", "retained_accounting"}
    if (not isinstance(result, dict) or not required <= set(result) or set(result) - required - optional
            or result["status"] != "failed" or not isinstance(result["error_type"], str) or not result["error_type"]
            or result["known_cost"] is not None or result["usage_incomplete"] is not True
            or result["details"] != "see operation logs and native receipts"
            or ("failure_classification" in result and result["failure_classification"] != "infrastructure")
            or (output / "failure.json").read_bytes() != encode(result)
            or not _file(output / "result.json") or (output / "result.json").read_bytes() != encode(result)
            or not _file(output / "failure-traceback.txt")):
        raise GrowthError("noncanonical operation failure cannot close a slot")
    for key, filename in (("retained_usage", "usage.json"), ("retained_accounting", "accounting.json")):
        if key in result and (not _file(output / filename) or encode(read(output / filename)) != encode(result[key])):
            raise GrowthError("retained failure accounting changed")
    return True


def run(root, *, execution, registry_ref, backend, qualify, expected_catalog_sha256):
    """Execute or audit the registered schedule; return only counts and status.

    Backend.root must be this root: its evaluate method owns native subprocess
    cancellation/cleanup. The trusted qualifier is required on each admission
    and each result revalidation. Removing CANCEL does not clear durable halts.
    """
    root = _root(root)
    if not callable(qualify) or not callable(getattr(backend, "evaluate", None)) or not callable(getattr(backend, "validate_evaluation", None)):
        raise GrowthError("native backend and external trusted qualification callback are required")
    if _root(getattr(backend, "root", None)) != root:
        raise GrowthError("native backend must observe the execution root's CANCEL signal")
    execution = json.loads(encode(execution))
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        registration = validate_execution(execution, registry_ref=registry_ref,
                                          expected_catalog_sha256=expected_catalog_sha256, require_active=True)
        from scripts.growth_final_native import require_bound_backend
        require_bound_backend(backend, qualify, root, registration)
        record(root / "execution.json", execution)
        record(root / "invocation.json", {"schema_version": 1, "execution_sha256": sha256(encode(execution)),
                                          "root": str(root), "registry_ref": registry_ref})
        state = _Execution(root, execution, registry_ref, backend, qualify)
        state.preflight()
        if not state.stopped():
            state.dispatch(registration["pairing"]["active_blocks"])
        return state.finish()


class _Execution:
    def __init__(self, root, execution, registry_ref, backend, qualify):
        self.root, self.execution, self.registry_ref = root, execution, registry_ref
        self.backend, self.qualify = backend, qualify
        self.digest = sha256(encode(execution))
        self.guard = threading.Lock()
        self.states = {entry["slot"]["slot"]: "not-started" for entry in execution["slots"]}
        self.halted = threading.Event()

    def halt(self, reason, source):
        with self.guard:
            self.halted.set()
            value = {"schema_version": 1, "execution_sha256": self.digest,
                     "reason": reason, "source": source}
            # Separate cause receipts keep a later infrastructure fault visible
            # even when a user cancellation was observed first.
            if not _file(self.root / f"halt-{reason}.json"):
                record(self.root / f"halt-{reason}.json", value)
            if not _file(self.root / "halt.json"):
                record(self.root / "halt.json", value)
            if not _file(self.root / "CANCEL"):
                seal(self.root / "CANCEL", b"Final execution stopped; see halt receipts.\n")

    def stopped(self):
        if self.halted.is_set():
            return True
        if any(_file(self.root / name) for name in ("halt.json", "halt-user.json", "halt-infra.json", "halt-exception.json")):
            self.halted.set()
            if not _file(self.root / "CANCEL"):
                seal(self.root / "CANCEL", b"Final execution remains stopped; see halt receipts.\n")
            return True
        if _file(self.root / "CANCEL"):
            self.halt("user", "CANCEL")
            return True
        return False

    def qualified(self, entry, output):
        candidate = json.loads(encode(entry))
        self.qualify(candidate["slot"], candidate["specification"], output)
        if encode(candidate) != encode(entry):
            raise GrowthError("trusted qualifier mutated a frozen slot specification")

    def verify(self, entry):
        output = validate_slot_binding(self.root, self.execution, entry, execution_sha256=self.digest)
        self.qualified(entry, output)
        result = read(output / "result.json")
        if (output / "result.json").read_bytes() != encode(result):
            raise GrowthError("retained result differs from the canonical operation encoding")
        if validate_failure(output, result):
            if result.get("failure_classification") == "infrastructure":
                self.halt("infra", entry["slot"]["slot"])
            return "failed"
        specification, checked = json.loads(encode(entry["specification"])), json.loads(encode(result))
        actual = self.backend.validate_evaluation(output, specification, checked)
        if encode(specification) != encode(entry["specification"]) or encode(checked) != encode(result):
            raise GrowthError("native revalidation mutated a frozen specification or result")
        if encode(actual) != encode(result):
            raise GrowthError("backend revalidation differs from the sealed canonical result")
        return "evaluated"

    def evaluate(self, entry, output):
        specification = json.loads(encode(entry["specification"]))
        result = self.backend.evaluate(output, specification)
        if encode(specification) != encode(entry["specification"]):
            raise GrowthInfrastructureError("native execution mutated its frozen specification")
        return result

    def rejection(self, entry, error):
        output = _output(self.root, entry)
        output.mkdir(parents=True, exist_ok=True)
        if not _file(output / "execution-error.json"):
            record(output / "execution-error.json", {"schema_version": 1, "error_type": type(error).__name__,
                   "slot": entry["slot"]["slot"], "details": "native qualification or execution evidence rejected"})
        self.halt("infra" if isinstance(error, GrowthInfrastructureError) else "exception", entry["slot"]["slot"])
        return "unverified"

    def preflight(self):
        for entry in self.execution["slots"]:
            output = _output(self.root, entry)
            index = entry["slot"]["slot"]
            try:
                if _file(output / "execution-error.json"):
                    self.states[index] = "unverified"
                    self.halt("exception", index)
                elif _file(output / "result.json"):
                    self.states[index] = self.verify(entry)
                elif _file(output / "started.json"):
                    # A cancellation receipt does not prove a native result or
                    # allow a fresh deadline; preserve the unresolved admission.
                    self.states[index] = "ambiguous"
                    if not self.stopped():
                        self.halt("exception", index)
            except KeyboardInterrupt:
                self.halt("user", "host-interrupt")
                return
            except Exception as error:
                self.states[index] = self.rejection(entry, error)

    def worker(self, entry):
        output = _output(self.root, entry)
        index = entry["slot"]["slot"]
        try:
            if self.stopped():
                return "not-started"
            self.qualified(entry, output)
            if self.stopped():
                return "not-started"
            operation(output, KIND, entry["specification"], lambda: self.evaluate(entry, output))
            return self.verify(entry)
        except KeyboardInterrupt:
            if not self.stopped():
                self.halt("user", index)
            record(output / "cancellation.json", {"schema_version": 1, "slot": index,
                   "reason": "native-interrupted", "result_present": _file(output / "result.json"),
                   "closure": "requires-native-reconciliation"})
            return "ambiguous" if _file(output / "started.json") else "not-started"
        except GrowthInfrastructureError as error:
            self.halt("infra", index)
            if _file(output / "result.json"):
                try:
                    return self.verify(entry)
                except Exception as invalid:
                    return self.rejection(entry, invalid)
            return self.rejection(entry, error)
        except BaseException as error:
            return self.rejection(entry, error)

    def dispatch(self, active_blocks):
        blocks = [self.execution["slots"][index:index + 2] for index in range(0, 800, 2)
                  if any(self.states[entry["slot"]["slot"]] == "not-started"
                         for entry in self.execution["slots"][index:index + 2])]
        next_block, pending = 0, {}
        # The context waits for native cleanup. No Future.cancel claim is used
        # as evidence that an already launched native subprocess has stopped.
        with ThreadPoolExecutor(max_workers=2 * active_blocks, thread_name_prefix="final-native") as executor:
            while pending or (next_block < len(blocks) and not self.stopped()):
                try:
                    active = {block for block, _ in pending.values()}
                    while len(active) < active_blocks and next_block < len(blocks) and not self.stopped():
                        with _database(self.registry_ref) as connection:
                            events = connection.execute("SELECT state FROM event WHERE attempt_index=?",
                                (self.execution["registry_receipt"]["attempt_index"],)).fetchall()
                        if events:
                            self.halt("user" if all(row[0] == "cancelled" for row in events) else "exception", "registry-event")
                            break
                        block = blocks[next_block]
                        next_block += 1
                        for entry in block:
                            index = entry["slot"]["slot"]
                            if self.states[index] != "not-started" or self.stopped():
                                continue
                            output = _output(self.root, entry)
                            output.mkdir(parents=True, exist_ok=True)
                            record(output / "slot.json", slot_identity(self.execution, entry, execution_sha256=self.digest))
                            record(output / "dispatch.json", {"schema_version": 1, "slot": index,
                                   "block": entry["slot"]["block"], "position": entry["slot"]["position"],
                                   "order": "host-submission", "execution_sha256": self.digest})
                            pending[executor.submit(self.worker, entry)] = (entry["slot"]["block"], entry)
                            active.add(entry["slot"]["block"])
                    if pending:
                        done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in done:
                            _, entry = pending.pop(future)
                            self.states[entry["slot"]["slot"]] = future.result()
                except KeyboardInterrupt:
                    self.halt("user", "host-interrupt")
                except BaseException:
                    # Admission/control-plane failures stop fresh work while
                    # existing backend owners complete their cancellation path.
                    self.halt("exception", "host-dispatch")
                    for future in list(pending):
                        if future.done():
                            _, entry = pending.pop(future)
                            try:
                                self.states[entry["slot"]["slot"]] = future.result()
                            except BaseException as error:
                                self.states[entry["slot"]["slot"]] = self.rejection(entry, error)

    def finish(self):
        inventory = []
        for entry in self.execution["slots"]:
            output = _output(self.root, entry)
            index = entry["slot"]["slot"]
            # A host interrupt can occur after submit() but before its Future is
            # put in pending. Executor shutdown still drains that worker; derive
            # its retained state instead of calling it not-started.
            if self.states[index] in {"not-started", "ambiguous"} and _file(output / "result.json"):
                try:
                    self.states[index] = self.verify(entry)
                except Exception as error:
                    self.states[index] = self.rejection(entry, error)
            if self.states[index] == "not-started" and _file(output / "started.json"):
                self.states[index] = "ambiguous"
            inventory.append({"slot": entry["slot"], "output": entry["output"],
                              "state": self.states[index],
                              **{name: _file(output / filename) for name, filename in (
                                  ("started", "started.json"), ("result", "result.json"),
                                  ("failure", "failure.json"), ("cancelled", "cancellation.json"))}})
        counts = {"assigned": 800, **{name: sum(row["state"] == name for row in inventory)
                  for name in ("evaluated", "failed", "ambiguous", "unverified", "not-started")},
                  "results_present": sum(row["result"] for row in inventory),
                  "cancelled": sum(row["cancelled"] for row in inventory)}
        status = ("infrastructure-failed" if _file(self.root / "halt-infra.json") else
                  "halted" if _file(self.root / "halt-exception.json") else
                  "cancelled" if _file(self.root / "halt-user.json") else
                  "closed" if counts["evaluated"] + counts["failed"] == counts["results_present"] == 800 else "incomplete")
        summary = {"schema_version": 1, "status": status, "counts": counts}
        output = self.root / "executions" / uuid.uuid4().hex
        record(output / "inventory.json", {"schema_version": 1, "execution_sha256": self.digest,
                                          "slots": inventory})
        record(output / "summary.json", summary)
        return summary


def _root(value):
    if not isinstance(value, (str, Path)):
        raise GrowthError("execution requires a canonical absolute root")
    root = _path(value, exists=False)
    if root.exists() and not root.is_dir():
        raise GrowthError("execution root must be a directory")
    return root


def _output(root, entry):
    output = root / entry["output"]
    if output != output.resolve() or output.is_symlink() or (output.exists() and not output.is_dir()):
        raise GrowthError("slot output must remain inside its canonical execution root")
    return output


def _file(path):
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise GrowthError("execution evidence must be a regular non-symlink file")
    return True


@contextmanager
def _lock(root):
    descriptor = os.open(root / ".execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrowthError("execution lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GrowthError("another final execution owns this cohort") from error
        yield
    finally:
        os.close(descriptor)
