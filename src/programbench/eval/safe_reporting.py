"""Keep pytest-timeout's signal budget on execution, outside report rendering.

The active timer's remaining duration is preserved across each makereport boundary.
Setup, call, and teardown still consume that same remaining budget. Assertions and
reports are not suppressed; a timeout raised by execution remains a failure/error.
"""

import signal
import threading

import pytest
import pytest_timeout

_REMAINING = pytest.StashKey()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # Rerunfailures reuses the protocol wrapper after the signal timer has fired.
    if _REMAINING in item.stash:
        del item.stash[_REMAINING]
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        return
    settings = pytest_timeout._get_item_settings(item)
    if (getattr(item, "execution_count", 1) > 1 and settings.method == "signal"
            and settings.func_only is False and settings.timeout is not None and settings.timeout > 0
            and signal.getitimer(signal.ITIMER_REAL)[0] <= 0):
        item.config.hook.pytest_timeout_set_timer(item=item, settings=settings)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM"):
        handler = signal.getsignal(signal.SIGALRM)
        if getattr(handler, "__module__", None) == "pytest_timeout":
            remaining, interval = signal.setitimer(signal.ITIMER_REAL, 0)
            if remaining > 0:
                item.stash[_REMAINING] = (remaining, interval, handler)
    yield


def _resume(item):
    if _REMAINING not in item.stash:
        return
    remaining, interval, handler = item.stash[_REMAINING]
    del item.stash[_REMAINING]
    if signal.getsignal(signal.SIGALRM) is handler:
        signal.setitimer(signal.ITIMER_REAL, remaining, interval)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _resume(item)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _resume(item)
