import pytest

from programbench.container import ContainerEnvironment


@pytest.mark.slow
def test_docker_timeout_retains_captured_failure_diagnostics():
    env = ContainerEnvironment(
        image="programbench/bootandy_1776_dust.62bf1e1:task_cleanroom_v6",
        cpus=1,
        run_args=["--network", "none"],
    )
    try:
        result = env.execute("printf 'before-timeout\\n'; sleep 10", timeout=1)
        assert result["returncode"] == -1
        assert "before-timeout" in result["output"]
        assert "timed out" in result["exception_info"]
    finally:
        env.cleanup()
