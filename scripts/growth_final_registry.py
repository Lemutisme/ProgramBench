"""Host-owned final-confirmation registration; never admits or scores solver work.

The external owner must retain ONE registry and its returned identity across all
attempts. SQLite FULL-synchronous commits allocate j before it is returned; an
exact retry after a committed-but-unobserved response recovers that same j.
Uncommitted transactions have not allocated an externally usable j. Cancellation
and failure append events, never delete registrations or refund alpha.

This binds a separately trusted 200-ID catalog and complete revision-manifest
envelopes. It does not authenticate the catalog, recursively requalify revisions,
verify execution/deadlines, unblind results, or confer adoption/launch authority.
SQLite/file ownership is a trust boundary: it cannot detect a privileged owner
restoring an internally consistent old backup or creating another registry.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from scripts.growth_loop import GrowthError, encode, frozen_input, record, sha256


ALGORITHM = "sha256-fisher-yates-rejection-v1"
SCORE_ENCODING = "official-resolved-total-v1"
EXECUTION = {"wall_time_seconds": 21600, "deadline_origin": "first_admission", "extend_on_resume": False}
ARMS = ("baseline", "candidate")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_SCHEMA = (
    "CREATE TABLE registry (identity TEXT NOT NULL, schema_version INTEGER NOT NULL)",
    "CREATE TABLE attempt (j INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT UNIQUE NOT NULL, "
    "registration_sha256 TEXT UNIQUE NOT NULL, registration BLOB NOT NULL, previous_sha256 TEXT NOT NULL, "
    "receipt BLOB NOT NULL, receipt_sha256 TEXT UNIQUE NOT NULL)",
    "CREATE TABLE event (id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_index INTEGER NOT NULL REFERENCES attempt(j), "
    "state TEXT NOT NULL CHECK(state IN ('cancelled','failed')), recorded_ns INTEGER NOT NULL)",
    *(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
      "BEGIN SELECT RAISE(ABORT, 'registry is append-only'); END"
      for table in ("registry", "attempt", "event") for action in ("UPDATE", "DELETE")),
    "CREATE TRIGGER registry_no_insert BEFORE INSERT ON registry WHEN EXISTS (SELECT 1 FROM registry) "
    "BEGIN SELECT RAISE(ABORT, 'registry identity is immutable'); END",
)


def alpha(attempt_index: int) -> dict:
    if type(attempt_index) is not int or attempt_index < 1:
        raise GrowthError("attempt_index must be a positive integer")
    return {"numerator": 1, "denominator": 20 * attempt_index * (attempt_index + 1)}


def _keys(value: object, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise GrowthError(f"invalid {label} fields")


def _hash(value: object) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise GrowthError("invalid SHA256")


def _path(value: object, *, exists: bool = True) -> Path:
    if not isinstance(value, (str, Path)):
        raise GrowthError("path must be canonical and absolute")
    path = Path(value)
    if not path.is_absolute() or str(value) != str(path) or str(path) != str(path.resolve()) or path.is_symlink():
        raise GrowthError("path must be canonical and absolute without symlinks")
    if exists and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
        raise GrowthError("path must name an existing regular file")
    return path


def _reference(value: object, *, verify: bool) -> bytes | None:
    _keys(value, {"path", "sha256"}, "artifact reference")
    _hash(value["sha256"])
    _path(value["path"], exists=verify)
    return frozen_input(value) if verify else None


def _json(data: bytes) -> object:
    try:
        value = json.loads(data)
        if encode(value) != data:
            raise GrowthError("registration must use canonical frozen encoding")
        return value
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        raise GrowthError("invalid canonical registration JSON") from error


def _tasks(value: object) -> list[str]:
    if (not isinstance(value, list) or len(value) != 200
            or any(not isinstance(task, str) or not _NAME.fullmatch(task) for task in value)
            or len(set(value)) != 200):
        raise GrowthError("catalog and registration require 200 unique task IDs")
    return value


def paired_schedule(task_ids: list[str], seed: str) -> list[dict]:
    """SHA256 counter stream + rejection sampling + descending Fisher-Yates.

    The seed is exactly 32 bytes encoded as lower-case hex. Input tasks are
    sorted before sampling. Draw all first-repeat arm orders, then shuffle task
    order for repeat 1 and repeat 2. A slot is an allocation, never a tool cap.
    """
    tasks = sorted(_tasks(task_ids))
    _hash(seed)
    counter = 0

    def draw(size: int) -> int:
        nonlocal counter
        limit = (1 << 256) - ((1 << 256) % size)
        while True:
            digest = hashlib.sha256(ALGORITHM.encode() + b"\0" + bytes.fromhex(seed)
                                    + counter.to_bytes(16, "big")).digest()
            counter += 1
            number = int.from_bytes(digest, "big")
            if number < limit:
                return number % size

    first = {task: draw(2) for task in tasks}
    slots = []
    for repeat in (1, 2):
        ordered = tasks.copy()
        for index in range(len(ordered) - 1, 0, -1):
            selected = draw(index + 1)
            ordered[index], ordered[selected] = ordered[selected], ordered[index]
        for task in ordered:
            order = first[task] if repeat == 1 else 1 - first[task]
            block = len(slots) // 2 + 1
            for position, arm in enumerate(ARMS if order == 0 else ARMS[::-1], 1):
                slots.append({"slot": len(slots) + 1, "block": block, "position": position,
                              "task_id": task, "repeat": repeat, "arm": arm})
    return slots


def _manifest(reference: dict) -> None:
    """Check the full manifest envelope, not validate_revision qualification."""
    try:
        value = json.loads(_reference(reference, verify=True))
        if (type(value.get("version")) is not int or value["version"] != 1 or value.get("kind") != "agent-revision"
                or value.get("status") != "qualified" or value.get("adoption_authority") is not False):
            raise GrowthError("registration requires a qualified AgentRevision manifest envelope")
        for name in ("changed_paths", "components"):
            if not isinstance(value[name], list) or any(not isinstance(item, str) for item in value[name]):
                raise GrowthError("revision change identity is missing")
        if value["parent"] is not None:
            _reference(value["parent"], verify=False)
        for name in ("binary", "solver_policy", "generator_policy", "recipe", "source_patch", "proposal",
                     "build_receipt", "runtime_options_file"):
            _reference(value[name], verify=False)
        source = value["source"]
        for name in ("archive", "files"):
            _reference(source[name], verify=False)
        _hash(source["tree_sha256"])
        if source["files"]["sha256"] != source["tree_sha256"]:
            raise GrowthError("revision source inventory identity conflicts")
        for origin in (source, source["base"]):
            _path(origin["repo"], exists=False)
            if not isinstance(origin["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", origin["commit"]):
                raise GrowthError("revision source commit is missing")
        if not isinstance(value["runtime_options"], dict) or set(value["runtime_options"]) != {"experiments"}:
            raise GrowthError("revision runtime options are missing")
        if (value["runtime_options"]["experiments"] is not None
                and not isinstance(value["runtime_options"]["experiments"], dict)):
            raise GrowthError("revision runtime options have an invalid shape")
        if not isinstance(value["qualification_receipts"], list) or not value["qualification_receipts"]:
            raise GrowthError("revision qualification references are missing")
        for item in value["qualification_receipts"]:
            _reference(item, verify=False)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise GrowthError("incomplete AgentRevision manifest envelope") from error


def validate_registration(value: dict, *, expected_catalog_sha256: str, verify_inputs: bool = True) -> dict:
    _keys(value, {"schema_version", "attempt_id", "revisions", "catalog", "analyzer", "configuration", "model",
                  "execution", "pairing", "direction", "score_encoding", "task_ids", "repeats", "schedule"},
          "registration")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise GrowthError("unsupported registration schema")
    if not isinstance(value["attempt_id"], str) or not _NAME.fullmatch(value["attempt_id"]):
        raise GrowthError("invalid external attempt identity")
    _keys(value["revisions"], set(ARMS), "revision arms")
    for reference in value["revisions"].values():
        _reference(reference, verify=False)
        if verify_inputs:
            _manifest(reference)
    if value["revisions"]["baseline"]["sha256"] == value["revisions"]["candidate"]["sha256"]:
        raise GrowthError("baseline and candidate must bind different revision manifests")
    if value["score_encoding"] != SCORE_ENCODING:
        raise GrowthError("final confirmation requires exact official resolved/total scores")
    _hash(expected_catalog_sha256)
    catalog = _reference(value["catalog"], verify=verify_inputs)
    if value["catalog"]["sha256"] != expected_catalog_sha256:
        raise GrowthError("catalog differs from externally designated hash")
    tasks = _tasks(value["task_ids"])
    if tasks != sorted(tasks):
        raise GrowthError("registered task IDs must be sorted")
    if verify_inputs:
        try:
            entries = json.loads(catalog)
            if not isinstance(entries, list):
                raise GrowthError("catalog must be a separately frozen list, not official metadata mapping")
            catalog_tasks = _tasks([entry["instance_id"] for entry in entries])
        except (KeyError, TypeError, ValueError) as error:
            raise GrowthError("invalid designated task catalog") from error
        if set(catalog_tasks) != set(tasks):
            raise GrowthError("registration must include exactly the designated catalog")
    for name in ("analyzer", "configuration"):
        _reference(value[name], verify=verify_inputs)
    _keys(value["model"], {"provider_id", "model_id", "effort"}, "model")
    if any(not isinstance(item, str) or not item.strip() for item in value["model"].values()):
        raise GrowthError("exact model and effort identities are required")
    if encode(value["execution"]) != encode(EXECUTION):
        raise GrowthError("execution must declare the original six-hour deadline only")
    if type(value["repeats"]) is not int or value["repeats"] != 2 or value["direction"] != "candidate-baseline":
        raise GrowthError("two repeats and candidate-baseline direction are fixed")
    _keys(value["pairing"], {"seed", "algorithm", "solver_workers", "active_blocks"}, "pairing")
    pairing = value["pairing"]
    if (pairing["algorithm"] != ALGORITHM or type(pairing["solver_workers"]) is not int
            or type(pairing["active_blocks"]) is not int or pairing["active_blocks"] < 1
            or pairing["solver_workers"] != 2 * pairing["active_blocks"]):
        raise GrowthError("pairing requires a fixed algorithm and two workers per active pair")
    expected = paired_schedule(tasks, pairing["seed"])
    if encode(value["schedule"]) != encode(expected):
        raise GrowthError("schedule differs from the frozen 800-slot/400-block pairing")
    return value


def freeze_registration(path: Path, coordinates: dict, *, expected_catalog_sha256: str) -> dict:
    """Prepare a sealed schedule without allocating j or granting admission."""
    _keys(coordinates, {"attempt_id", "revisions", "catalog", "analyzer", "configuration", "model", "execution",
                        "pairing", "task_ids"}, "registration coordinates")
    value = {**coordinates, "schema_version": 1, "task_ids": sorted(_tasks(coordinates["task_ids"])), "repeats": 2,
             "direction": "candidate-baseline", "score_encoding": SCORE_ENCODING,
             "schedule": paired_schedule(coordinates["task_ids"], coordinates["pairing"]["seed"])}
    validate_registration(value, expected_catalog_sha256=expected_catalog_sha256)
    path = _path(path, exists=False)
    record(path, value)
    return {"path": str(path), "sha256": sha256(encode(value))}


@contextmanager
def _database(reference: dict):
    _keys(reference, {"path", "identity"}, "registry reference")
    if not isinstance(reference["identity"], str) or not re.fullmatch(r"[0-9a-f]{32}", reference["identity"]):
        raise GrowthError("invalid registry identity")
    path = _path(reference["path"])
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise GrowthError("registry integrity check failed")
        schema = {row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if schema != set(_SCHEMA) | {"CREATE TABLE sqlite_sequence(name,seq)"}:
            raise GrowthError("registry schema changed or initialization was interrupted")
        if connection.execute("SELECT identity,schema_version FROM registry").fetchall() != [
            (reference["identity"], 1)
        ]:
            raise GrowthError("registry identity changed")
        _audit(connection, reference["identity"])
        yield connection
        connection.commit()
    except GrowthError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
        raise GrowthError("registry unavailable or corrupt; preserve it and fail closed") from error
    finally:
        if connection is not None:
            connection.close()


def initialize_registry(path: Path) -> dict:
    """One-time external operation; never call automatically from register()."""
    path = _path(path, exists=False)
    identity = uuid.uuid4().hex
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.fsync(descriptor)
    os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute("INSERT INTO registry VALUES (?,1)", (identity,))
            connection.commit()
        finally:
            connection.close()
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"path": str(path), "identity": identity}


def _audit(connection: sqlite3.Connection, identity: str) -> None:
    previous = "0" * 64
    count = 0
    for row in connection.execute("SELECT * FROM attempt ORDER BY j"):
        j, attempt_id, digest, raw, parent, receipt, receipt_hash = row
        value = _json(raw)
        expected = {"schema_version": 1, "registry_identity": identity, "attempt_index": j,
                    "attempt_id": attempt_id, "registration_sha256": digest, "alpha": alpha(j),
                    "previous_sha256": previous, "direction": "candidate-baseline", "launch_authority": False}
        if (j != count + 1 or sha256(raw) != digest or value["attempt_id"] != attempt_id or parent != previous
                or receipt != encode(expected) or sha256(receipt) != receipt_hash):
            raise GrowthError("registry allocation chain is damaged; no number may be reused")
        validate_registration(value, expected_catalog_sha256=value["catalog"]["sha256"], verify_inputs=False)
        previous = receipt_hash
        count += 1
    sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='attempt'").fetchall()
    if sequence != ([(count,)] if count else []):
        raise GrowthError("registry high-water mark conflicts with retained allocations")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise GrowthError("registry event ownership is damaged")
    events = connection.execute("SELECT id,state,recorded_ns FROM event ORDER BY id").fetchall()
    if any(row[0] != index or row[1] not in {"cancelled", "failed"} or type(row[2]) is not int or row[2] <= 0
           for index, row in enumerate(events, 1)):
        raise GrowthError("registry event history is damaged")
    event_sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='event'").fetchall()
    if event_sequence != ([(len(events),)] if events else []):
        raise GrowthError("registry event high-water mark conflicts with retained events")


def register(reference: dict, registration: dict, *, expected_catalog_sha256: str) -> dict:
    """Allocate once after input verification; exact sealed retries reuse j."""
    raw = _reference(registration, verify=True)
    value = validate_registration(_json(raw), expected_catalog_sha256=expected_catalog_sha256)
    with _database(reference) as connection:
        existing = connection.execute("SELECT registration_sha256,receipt FROM attempt WHERE attempt_id=?",
                                      (value["attempt_id"],)).fetchone()
        if existing:
            if existing[0] != registration["sha256"]:
                raise GrowthError("attempt identity conflicts with its sealed registration")
            result = _json(existing[1])
        else:
            previous = connection.execute("SELECT receipt_sha256 FROM attempt ORDER BY j DESC LIMIT 1").fetchone()
            index = connection.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] + 1
            result = {"schema_version": 1, "registry_identity": reference["identity"], "attempt_index": index,
                      "attempt_id": value["attempt_id"], "registration_sha256": registration["sha256"],
                      "alpha": alpha(index),
                      "previous_sha256": previous[0] if previous else "0" * 64,
                      "direction": "candidate-baseline", "launch_authority": False}
            cursor = connection.execute("INSERT INTO attempt "
                                        "(attempt_id,registration_sha256,registration,previous_sha256,"
                                        "receipt,receipt_sha256) VALUES (?,?,?,?,?,?)",
                                        (value["attempt_id"], registration["sha256"], raw, result["previous_sha256"],
                                         encode(result), sha256(encode(result))))
            if cursor.lastrowid != index:
                raise GrowthError("registry allocation index changed")
    # No registration receipt escapes before the FULL-synchronous commit.
    return result


def append_status(reference: dict, attempt_index: int, registration_sha256: str, state: str) -> None:
    """External cancellation/failure declarations; not execution validation."""
    alpha(attempt_index)
    _hash(registration_sha256)
    if state not in {"cancelled", "failed"}:
        raise GrowthError("only cancellation/failure metadata may be appended")
    with _database(reference) as connection:
        owner = connection.execute("SELECT registration_sha256 FROM attempt WHERE j=?", (attempt_index,)).fetchone()
        if owner != (registration_sha256,):
            raise GrowthError("status does not match its registered attempt")
        connection.execute("INSERT INTO event (attempt_index,state,recorded_ns) VALUES (?,?,?)",
                           (attempt_index, state, time.time_ns()))


def registrations(reference: dict) -> list[dict]:
    """Audited append-only allocation receipts, without scores or admission."""
    with _database(reference) as connection:
        return [_json(row[0]) for row in connection.execute("SELECT receipt FROM attempt ORDER BY j")]
