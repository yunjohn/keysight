import threading
import time

from keysight_scope_app.infra.task_runner import BackgroundTaskRunner


def _drain_until(runner: BackgroundTaskRunner, predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        runner.drain_ui_queue()
        time.sleep(0.005)
    runner.drain_ui_queue()


def test_latest_key_discards_stale_result() -> None:
    runner = BackgroundTaskRunner()
    release = threading.Event()
    results: list[str] = []
    runner.run(lambda: (release.wait(), "old")[1], on_success=results.append, task_key="capture")
    runner.run(lambda: "new", on_success=results.append, task_key="capture")
    release.set()
    _drain_until(runner, lambda: results == ["new"])
    assert results == ["new"]
    runner.shutdown()


def test_cancelled_task_does_not_deliver_callback() -> None:
    runner = BackgroundTaskRunner()
    release = threading.Event()
    results: list[str] = []
    handle = runner.run(lambda: (release.wait(), "done")[1], on_success=results.append)
    handle.cancel()
    release.set()
    handle.wait(1)
    runner.drain_ui_queue()
    assert results == []
    runner.shutdown()
