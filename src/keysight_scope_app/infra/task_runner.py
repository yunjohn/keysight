from __future__ import annotations

from queue import Empty, Queue
import threading
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


UiCallback = Callable[[], None]


class RepeatingTaskHandle:
    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()


class BackgroundTaskRunner:
    def __init__(self) -> None:
        self._ui_queue: Queue[UiCallback] = Queue()

    def post_ui(self, callback: UiCallback) -> None:
        self._ui_queue.put(callback)

    def drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except Empty:
                break
            callback()

    def run(
        self,
        task: Callable[[], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_finally: UiCallback | None = None,
    ) -> None:
        def worker() -> None:
            try:
                result = task()
            except Exception as exc:
                if on_error is not None:
                    self.post_ui(lambda error=exc: on_error(error))
                if on_finally is not None:
                    self.post_ui(on_finally)
                return

            if on_success is not None:
                self.post_ui(lambda value=result: on_success(value))
            if on_finally is not None:
                self.post_ui(on_finally)

        threading.Thread(target=worker, daemon=True).start()

    def run_repeating(
        self,
        task: Callable[[], T],
        *,
        interval_s: float,
        on_result: Callable[[T], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_stopped: UiCallback | None = None,
    ) -> RepeatingTaskHandle:
        stop_event = threading.Event()

        def worker() -> None:
            while not stop_event.is_set():
                try:
                    result = task()
                except Exception as exc:
                    stop_event.set()
                    if on_error is not None:
                        self.post_ui(lambda error=exc: on_error(error))
                    break

                if on_result is not None:
                    self.post_ui(lambda value=result: on_result(value))

                if stop_event.wait(interval_s):
                    break

            if on_stopped is not None:
                self.post_ui(on_stopped)

        threading.Thread(target=worker, daemon=True).start()
        return RepeatingTaskHandle(stop_event)
