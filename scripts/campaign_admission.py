"""Host disk guard for new work; never cancels or rebudgets admitted work."""

import json
from pathlib import Path
import shutil
import time

from scripts.campaign_install import _atomic_write_json


def disk_admission(root: Path, minimum: int | None, receipt: Path) -> bool:
    if minimum is None:
        return True
    if type(minimum) is not int or minimum <= 0:
        raise ValueError("minimum disk free bytes must be a positive integer")
    free = shutil.disk_usage(root).free
    allowed = free >= minimum
    previous = json.loads(receipt.read_bytes()) if receipt.exists() else {}
    if (previous.get("allowed"), previous.get("minimum_free_bytes"), previous.get("root")) != (
        allowed, minimum, str(root.resolve())
    ):
        _atomic_write_json(receipt, {"root": str(root.resolve()), "minimum_free_bytes": minimum,
                              "observed_free_bytes": free, "allowed": allowed, "time": time.time(),
                              "scope": "new admission only; active tasks retain original deadlines"})
    return allowed


def study_disk_minimum(study) -> int | None:
    guard = study.get("infrastructure_guard")
    if guard is None:
        return None
    if (not isinstance(guard, dict) or set(guard) != {"minimum_disk_free_bytes"}
            or type(guard["minimum_disk_free_bytes"]) is not int or guard["minimum_disk_free_bytes"] <= 0):
        raise ValueError("study infrastructure_guard requires positive minimum_disk_free_bytes only")
    return guard["minimum_disk_free_bytes"]
