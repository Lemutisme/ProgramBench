"""Prewarm every frozen inference image before campaign job admission."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


class PrewarmError(RuntimeError):
    pass


def image_references(manifest: object) -> tuple[str, ...]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("jobs"), list):
        raise PrewarmError("campaign manifest has invalid jobs")
    images = []
    for job in manifest["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("image"), str) or not job["image"]:
            raise PrewarmError("campaign manifest has invalid image reference")
        images.append(f"{job['image']}:task_cleanroom")
    values = tuple(sorted(set(images)))
    if not values:
        raise PrewarmError("campaign manifest has no inference images")
    return values


def ensure_image(reference: str) -> tuple[str, str]:
    digest = inspect_image(reference)
    if digest is None:
        try:
            pulled = subprocess.run(
                ("docker", "pull", reference),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PrewarmError(f"image pull failed: {reference}") from error
        if pulled.returncode != 0:
            raise PrewarmError(f"image pull failed: {reference}")
        digest = inspect_image(reference)
    if digest is None:
        raise PrewarmError(f"image digest unavailable: {reference}")
    return reference, digest


def inspect_image(reference: str) -> str | None:
    try:
        result = subprocess.run(
            ("docker", "image", "inspect", "--format", "{{.Id}}", reference),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value.startswith("sha256:") else None


def atomic_json(path: Path, value: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as temporary:
            json.dump(value, temporary, allow_nan=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    root = args.campaign.resolve()
    manifest_path = root / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    references = image_references(manifest)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lock_path = root / "prewarm.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        completed: dict[str, str] = {}
        failed: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(ensure_image, reference): reference for reference in references}
            for future in concurrent.futures.as_completed(futures):
                reference = futures[future]
                try:
                    name, digest = future.result()
                    completed[name] = digest
                except PrewarmError:
                    failed.append(reference)
                print(
                    f"prewarm completed={len(completed)} failed={len(failed)} total={len(references)}",
                    flush=True,
                )
        atomic_json(
            root / "prewarm.json",
            {
                "version": 1,
                "status": "completed" if not failed else "failed",
                "campaign_manifest_sha256": manifest_sha256,
                "images": dict(sorted(completed.items())),
                "failed": sorted(failed),
            },
        )
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
