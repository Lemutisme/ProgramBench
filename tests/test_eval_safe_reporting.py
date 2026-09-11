import os
import subprocess
import sys
from pathlib import Path

import pytest

from programbench.eval.eval import parse_test_results


@pytest.mark.parametrize('phase', ['call', 'setup', 'teardown'])
def test_reporting_guard_keeps_execution_timeouts(tmp_path, phase):
    root = Path(__file__).parents[1] / 'src/programbench/eval'
    fixture = {'setup': '    time.sleep(0.5)\n    yield\n',
               'teardown': '    yield\n    time.sleep(0.5)\n',
               'call': '    yield\n'}[phase]
    (tmp_path/'test_timeout.py').write_text(
        'import time, pytest\n@pytest.fixture\ndef fixture():\n' + fixture +
        'def test_timeout(fixture):\n' + ('    time.sleep(0.5)\n' if phase == 'call' else '    assert True\n'))
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', str(tmp_path/'test_timeout.py'), '--timeout=0.2',
         '--timeout-method=signal', '--assert=plain', '-p', 'pytest_timeout', '-p', 'safe_reporting',
         f'--junitxml={tmp_path}/results.xml', '-q'],
        env={**os.environ, 'PYTHONPATH': str(root), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    records = parse_test_results((tmp_path/'results.xml').read_text()).test_results
    assert any(record.status in {'failure', 'error'} for record in records)
    assert all('pytest.internal' not in record.name for record in records)


def test_slow_reporting_does_not_turn_assertion_failure_into_internal_error(tmp_path):
    root = Path(__file__).parents[1] / 'src/programbench/eval'
    (tmp_path/'conftest.py').write_text(
        'import time, pytest\n@pytest.hookimpl(hookwrapper=True, trylast=True)\n'
        'def pytest_runtest_makereport(item, call):\n'
        '    if call.when == "call": time.sleep(0.5)\n    yield\n')
    (tmp_path/'test_failure.py').write_text('def test_failure():\n    assert False\ndef test_pass():\n    assert True\n')
    for enabled, expected in ((False, 3), (True, 1)):
        report = tmp_path / f'{enabled}.xml'
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', str(tmp_path/'test_failure.py'), '--timeout=0.2',
             '--timeout-method=signal', '--assert=plain', '-p', 'pytest_timeout',
             *(['-p', 'safe_reporting'] if enabled else []), f'--junitxml={report}', '-q'],
            env={**os.environ, 'PYTHONPATH': str(root), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if enabled:
            assert [r.status for r in parse_test_results(report.read_text())] == ['failure', 'passed']
