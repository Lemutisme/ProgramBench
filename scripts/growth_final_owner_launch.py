"""Single-launch owner for a frozen full AgentRevision RSI study; requires current qualification."""

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

import control

ROOT = Path(__file__).resolve().parent
EXECUTION = ROOT / "execution"
CODE = ROOT
PYTHON = "/home/duozhou/ProgramBench/.venv/bin/python"
ARGV = [PYTHON, str(ROOT / "controller.py"), "--cohort", str(ROOT)]


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    control.save(path, value)


def identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
    return {
        "pid": pid,
        "start_ticks": int(fields[19]),
        "pgid": os.getpgid(pid),
        "state": fields[0],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def active(expected):
    try:
        current = identity(expected["pid"])
        return current["state"] != "Z" and all(current[key] == expected[key] for key in ["pid", "pgid", "start_ticks", "boot_id"])
    except (FileNotFoundError, ProcessLookupError):
        return False


def state():
    value = {"frozen": (EXECUTION / "frozen.json").exists(), "cancel_requested": (EXECUTION / "CANCEL").exists(),
        "completed": (EXECUTION / "completed.json").exists(),
        "started_operations": sum((path / "started.json").is_file() for path in control.operation_paths(ROOT)),
        "closed_operations": sum((path / "result.json").is_file() for path in control.operation_paths(ROOT))}
    for name in ["SUPERVISOR_PROCESS.json", "CONTROLLER_PROCESS.json"]:
        if (ROOT / name).exists():
            owner = read(ROOT / name)
            value[name] = {**owner, "identity_alive": active(owner["identity"]) if owner.get("identity") else owner.get("identity_alive")}
    return value


def check():
    return control.check(ROOT)


def readout(code):
    control.readout(ROOT, code)


def request_cancel(reason):
    EXECUTION.mkdir(parents=True, exist_ok=True)
    marker = EXECUTION / "CANCEL"
    if not marker.exists():
        marker.write_text("Explicit owner cancellation; preserve original deadlines and all evidence.\n")
    path = ROOT / "CANCELLATION_REQUEST.json"
    if not path.exists():
        save(path, {"time": time.time(), "marker": str(marker), "reason": reason})


def stop_on_signal(signum, frame):
    raise KeyboardInterrupt(f"supervisor signal {signum}")


def group_active(pgid):
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rpartition(") ")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid:
            return True
    return False


def enable_subreaper():
    # A runner intentionally calls setsid(). Its process group is not ownership.
    # Linux reassigns orphan descendants to this owner, including double forks.
    libc = ctypes.CDLL(None, use_errno=True)
    enabled = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0 or enabled.value != 1:
        raise OSError(ctypes.get_errno(), "cannot establish Linux child-subreaper ownership")
    if not hasattr(libc, "pidfd_open") or not hasattr(libc, "pidfd_send_signal"):
        raise RuntimeError("owner cleanup requires identity-safe pidfd signals")
    descriptor = libc.pidfd_open(os.getpid(), 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "owner cannot open Linux pidfd")
    os.close(descriptor)


def owned_descendants():
    records = {}
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rpartition(") ")[2].split()
            records[int(path.name)] = {"pid": int(path.name), "start_ticks": int(fields[19]),
                "pgid": int(fields[2]), "state": fields[0], "boot_id": boot_id, "ppid": int(fields[1])}
        except (FileNotFoundError, ProcessLookupError):
            continue
    owned = {os.getpid()}
    while True:
        found = {pid for pid, record in records.items() if record["ppid"] in owned}
        if found.issubset(owned):
            return [record for pid, record in records.items() if pid in owned and pid != os.getpid()]
        owned.update(found)


def signal_owned(expected, signum):
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.pidfd_open(expected["pid"], 0)
    if descriptor < 0:
        if ctypes.get_errno() == 3:
            return False
        raise OSError(ctypes.get_errno(), "cannot open owned pidfd")
    try:
        if not active(expected):
            return False
        if libc.pidfd_send_signal(descriptor, int(signum), None, 0) != 0:
            if ctypes.get_errno() == 3:
                return False
            raise OSError(ctypes.get_errno(), "cannot signal owned pidfd")
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def close_owned(process, *, cooperative_grace=55, interrupt_grace=55, kill_grace=10):
    events = []
    observed = {}
    receipt = {"version": 1, "owner": identity(os.getpid()), "controller_pid": process.pid,
        "started_at": time.time(), "status": "closing", "signals": events,
        "scope": "Kernel-descendant host processes, independent of session/process-group changes.",
        "external_resources_verified": False}
    try:
        for signum, grace in ((None, cooperative_grace), (signal.SIGINT, interrupt_grace), (signal.SIGKILL, kill_grace)):
            until = time.monotonic() + grace
            sent = set()
            while True:
                process.poll()
                descendants = owned_descendants()
                for item in descendants:
                    observed[(item["pid"], item["start_ticks"])] = item
                    if item["state"] == "Z" and item["ppid"] == os.getpid() and item["pid"] != process.pid:
                        try:
                            os.waitpid(item["pid"], os.WNOHANG)
                        except ChildProcessError:
                            pass
                live = [item for item in descendants if item["state"] != "Z"]
                if not live:
                    process.wait(timeout=10)
                    receipt.update(status="closed", exit_code=process.returncode)
                    return process.returncode
                request_cancel("owner closing; cancel all owned descendants including detached runners")
                for item in live:
                    key = (item["pid"], item["start_ticks"])
                    # First interrupt direct children, so runner finally blocks can
                    # stop workspaces/gateways before their own children are signalled.
                    if signum is not None and key not in sent and (signum == signal.SIGKILL or item["ppid"] == os.getpid()):
                        if signal_owned(item, signum):
                            events.append({"identity": item, "signal": int(signum), "time": time.time()})
                        sent.add(key)
                # A dead controller cannot observe CANCEL. Interrupt its adopted
                # runner immediately instead of spending the cooperative grace idle.
                if time.monotonic() >= until or signum is None and process.returncode is not None:
                    break
                time.sleep(.1)
        raise RuntimeError("owned descendants remain active after bounded cancellation")
    except BaseException as error:
        receipt.update(status="cleanup_failed", error_type=type(error).__name__)
        raise
    finally:
        receipt.update(finished_at=time.time(), observed=list(observed.values()),
            remaining=[item for item in owned_descendants() if item["state"] != "Z"],
            forced=any(event["signal"] == signal.SIGKILL for event in events))
        save(ROOT / "OWNED_CLEANUP.json", receipt)


def supervise():
    with (ROOT / ".supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        launch = read(ROOT / "LAUNCH.json")
        assert not (ROOT / "SUPERVISOR_PROCESS.json").exists()
        assert launch["argv"] == ARGV and launch["cwd"] == str(CODE)
        assert secrets.compare_digest(
            launch["supervisor_nonce_sha256"],
            hashlib.sha256(os.environ.get("RSI_LAUNCH_NONCE", "").encode()).hexdigest(),
        )
        os.environ.pop("RSI_LAUNCH_NONCE", None)
        signal.signal(signal.SIGTERM, stop_on_signal)
        signal.signal(signal.SIGINT, stop_on_signal)
        save(
            ROOT / "SUPERVISOR_PROCESS.json",
            {
                "identity": identity(os.getpid()),
                "status": "running",
                "started_at": time.time(),
            },
        )
        code = None
        failure = None
        process = None
        try:
            check()
            before = state()
            assert not before["started_operations"] and not before["completed"] and not before["cancel_requested"]
            enable_subreaper()
            with (ROOT / "controller.log").open("xb") as log:
                process = subprocess.Popen(
                    ARGV,
                    cwd=CODE,
                    env=os.environ,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                save(
                    ROOT / "CONTROLLER_PROCESS.json",
                    {
                        "identity": identity(process.pid),
                        "argv": ARGV,
                        "cwd": str(CODE),
                        "status": "running",
                        "started_at": time.time(),
                    },
                )
                code = process.wait()
            save(
                ROOT / "CONTROLLER_PROCESS.json",
                {
                    **read(ROOT / "CONTROLLER_PROCESS.json"),
                    "status": "exited",
                    "exit_code": code,
                    "finished_at": time.time(),
                },
            )
        except BaseException as error:
            failure = type(error).__name__
            save(
                ROOT / "SUPERVISOR_FAILURE.json",
                {
                    "error_type": failure,
                    "time": time.time(),
                    "note": "No automatic retry; owner/process diagnostics retained separately.",
                },
            )
        finally:
            if process is not None:
                try:
                    code = close_owned(process)
                except BaseException as error:
                    failure = type(error).__name__
                    save(ROOT / "SUPERVISOR_CLEANUP_FAILURE.json", {"error_type": failure, "time": time.time()})
                previous = (
                    read(ROOT / "CONTROLLER_PROCESS.json")
                    if (ROOT / "CONTROLLER_PROCESS.json").exists()
                    else {"pid": process.pid}
                )
                save(
                    ROOT / "CONTROLLER_PROCESS.json",
                    {
                        **previous,
                        "status": "exited",
                        "exit_code": process.returncode,
                        "finished_at": time.time(),
                        "identity_alive": process.poll() is None,
                        "process_group_active": group_active(process.pid),
                        "owned_descendants_active": [item for item in owned_descendants() if item["state"] != "Z"],
                    },
                )
            try:
                readout(code)
            finally:
                save(
                    ROOT / "SUPERVISOR_PROCESS.json",
                    {
                        **read(ROOT / "SUPERVISOR_PROCESS.json"),
                        "status": "cleanup_failed" if failure and (ROOT / "SUPERVISOR_CLEANUP_FAILURE.json").exists() else "exited",
                        "controller_exit_code": code,
                        "failure_type": failure,
                        "finished_at": time.time(),
                    },
                )
        return code if code is not None and failure is None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--cancel", action="store_true")
    group.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.status:
        print(json.dumps(state(), indent=2))
        return 0
    if args.cancel:
        request_cancel("explicit --cancel")
        print(json.dumps({"cancel_requested": True}))
        return 0
    if args.supervise:
        return supervise()
    checked = check()
    if not args.start:
        print(json.dumps({**checked, "mode": "check-only", "native_admitted": False}))
        return 0
    with (ROOT / ".launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not (ROOT / "LAUNCH.json").exists(), (
            "A prior launch requires explicit reconciliation, never silent reissue."
        )
        before = state()
        assert not before["started_operations"] and not before["completed"] and not before["cancel_requested"]
        control.require_space(ROOT, "study-start", EXECUTION)
        assert os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in [
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "DOCKER_HOST",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
            ]
        }
        config = read(ROOT / "OWNER_CONFIG.json")
        resources = read(config["resources"]["path"])
        environment.update(
            PYTHONPATH=str(CODE) + os.pathsep + str(CODE / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            PROGRAMBENCH_RIPGREP=resources["ripgrep"]["path"],
            PROGRAMBENCH_BLOB_DIR=resources["blob_root"],
            OPENCODE_DISABLE_MODELS_FETCH="true",
            OPENCODE_SERVER_PASSWORD=secrets.token_hex(32),
            RSI_LAUNCH_NONCE=secrets.token_hex(32),
        )
        save(
            ROOT / "LAUNCH.json",
            {
                **checked,
                "status": "starting",
                "time": time.time(),
                "argv": ARGV,
                "cwd": str(CODE),
                "environment_names": sorted(environment),
                "secret_values_recorded": False,
                "supervisor_nonce_sha256": hashlib.sha256(environment["RSI_LAUNCH_NONCE"].encode()).hexdigest(),
            },
        )
        process = None
        try:
            enable_subreaper()
            with (ROOT / "supervisor.log").open("xb") as log:
                process = subprocess.Popen(
                    [PYTHON, str(Path(__file__).resolve()), "--supervise"],
                    cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True,
                )
            save(ROOT / "LAUNCH.json", {**read(ROOT / "LAUNCH.json"),
                "status": "supervisor-spawned", "supervisor": identity(process.pid)})
        except BaseException:
            if process is not None:
                close_owned(process)
            raise
        print(
            json.dumps(
                {
                    "started": True,
                    "supervisor": identity(process.pid),
                    "owner_record": str(ROOT / "LAUNCH.json"),
                }
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
