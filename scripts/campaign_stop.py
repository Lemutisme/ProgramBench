"""Cooperative process-local campaign stop signaling."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class CampaignStopped(RuntimeError):
    """Raised only at an adapter-owned safe stop boundary."""


@dataclass(frozen=True)
class ActiveJobHandle:
    value: int


@dataclass
class _ActiveJob:
    job: object
    force_stop: Callable[[], object]
    terminalize: Callable[[], object]
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _forced: bool = False
    _succeeded: bool | None = None
    _active: bool = True

    def force(self) -> bool | None:
        with self._lock:
            if not self._active:
                return None
            if self._forced:
                return self._succeeded is True
            self._forced = True
            failed = False
            try:
                self.force_stop()
            except BaseException:
                failed = True
            try:
                failed = self.terminalize() is False or failed
            except BaseException:
                failed = True
            self._succeeded = not failed
            return self._succeeded

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    def begin_bounded_force(self) -> tuple[Callable[[], object], Callable[[], object]] | bool | None:
        with self._lock:
            if not self._active:
                return None
            if self._forced:
                return self._succeeded is True
            self._forced = True
            return self.force_stop, self.terminalize

    def finish_bounded_force(self, succeeded: bool) -> None:
        with self._lock:
            self._succeeded = succeeded


class ActiveJobRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0
        self._active: dict[ActiveJobHandle, _ActiveJob] = {}

    def register(
        self,
        job: object,
        *,
        force_stop: Callable[[], object],
        terminalize: Callable[[], object],
    ) -> ActiveJobHandle:
        with self._lock:
            self._next += 1
            handle = ActiveJobHandle(self._next)
            self._active[handle] = _ActiveJob(job, force_stop, terminalize)
            return handle

    def unregister(self, handle: ActiveJobHandle) -> None:
        with self._lock:
            active = self._active.pop(handle, None)
        if active is not None:
            active.deactivate()

    def active_jobs(self) -> tuple[object, ...]:
        with self._lock:
            return tuple(entry.job for entry in self._active.values())

    def force_stop_all(self, *, deadline: float | None = None) -> tuple[object, ...]:
        with self._lock:
            active = tuple(self._active.values())
        if deadline is not None:
            return self._force_stop_all_bounded(active, deadline)
        failures = []
        for entry in active:
            if entry.force() is False:
                failures.append(entry.job)
        return tuple(failures)

    def _force_stop_all_bounded(self, active: tuple[_ActiveJob, ...], deadline: float) -> tuple[object, ...]:
        callbacks: list[tuple[_ActiveJob, list[tuple[threading.Thread, list[bool]]]]] = []
        failures = []
        for entry in active:
            claimed = entry.begin_bounded_force()
            if claimed is False:
                failures.append(entry.job)
                continue
            if not isinstance(claimed, tuple):
                continue
            helpers = []
            for name, callback in zip(("force", "terminal"), claimed, strict=True):
                result: list[bool] = []

                def run(operation: Callable[[], object] = callback, outcome: list[bool] = result) -> None:
                    try:
                        outcome.append(operation() is not False)
                    except BaseException:
                        outcome.append(False)

                thread = threading.Thread(
                    target=run,
                    name=f"campaign-{name}-callback",
                    daemon=True,
                )
                helpers.append((thread, result))
                thread.start()
            callbacks.append((entry, helpers))
        for _, helpers in callbacks:
            for thread, _ in helpers:
                thread.join(max(0.0, deadline - time.monotonic()))
        for entry, helpers in callbacks:
            succeeded = all(not thread.is_alive() and result == [True] for thread, result in helpers)
            entry.finish_bounded_force(succeeded)
            if not succeeded:
                failures.append(entry.job)
        return tuple(failures)


class StopToken:
    def __init__(self, event: threading.Event | None = None, registry: ActiveJobRegistry | None = None) -> None:
        self._event = event or threading.Event()
        self._lock = threading.Lock()
        self._reason = "campaign_stop" if self._event.is_set() else None
        self.registry = registry or ActiveJobRegistry()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason if self._reason is not None else "campaign_stop" if self._event.is_set() else None

    def set(self, reason: str = "campaign_stop") -> bool:
        sanitized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(reason).strip())[:128] or "campaign_stop"
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = sanitized
            self._event.set()
            return True

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def checkpoint(self) -> None:
        if self.is_set():
            raise CampaignStopped(self.reason or "campaign_stop")


class StopAwareApi:
    def __init__(self, target: object, token: StopToken) -> None:
        self.target = target
        self.token = token
        self._observations: list[tuple[str, object]] = []
        self._attempted: set[str] = set()
        self.blocked_method: str | None = None

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self.target, name)
        if not callable(attribute):
            return attribute

        def call(*args: object, **kwargs: object) -> object:
            if self.token.is_set():
                self.blocked_method = name
                self.token.checkpoint()
            self._attempted.add(name)
            result = attribute(*args, **kwargs)
            self._observations.append((name, result))
            return result

        return call

    def observed(self, name: str) -> object | None:
        return next((value for method, value in reversed(self._observations) if method == name), None)

    def attempted(self, name: str) -> bool:
        return name in self._attempted


def stop_aware_sleeper(token: StopToken, sleeper: Callable[[float], None]) -> Callable[[float], None]:
    def sleep(seconds: float) -> None:
        token.checkpoint()
        sleeper(seconds)
        token.checkpoint()

    return sleep
