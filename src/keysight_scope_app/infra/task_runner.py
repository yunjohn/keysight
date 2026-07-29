from __future__ import annotations

import threading
import uuid
from queue import Empty, Queue
from typing import Callable, TypeVar

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


class TaskHandle:
    def __init__(self, task_id: str, cancel_event: threading.Event, thread: threading.Thread) -> None:
        self.task_id = task_id
        self._cancel_event = cancel_event
        self._thread = thread

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and not self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


class BackgroundTaskRunner:
    def __init__(self) -> None:
        self._ui_queue: Queue[UiCallback] = Queue()
        self._lock = threading.Lock()
        self._handles: dict[str, TaskHandle] = {}
        self._latest_by_key: dict[str, str] = {}
        self._closed = False

    def post_ui(self, callback: UiCallback) -> None:
        if not self._closed:
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
        task_key: str | None = None,
    ) -> TaskHandle:
        task_id = uuid.uuid4().hex
        cancel_event = threading.Event()

        def should_deliver() -> bool:
            if cancel_event.is_set() or self._closed:
                return False
            if task_key is None:
                return True
            with self._lock:
                return self._latest_by_key.get(task_key) == task_id

        def deliver(callback: UiCallback) -> None:
            if should_deliver():
                self.post_ui(lambda: callback() if should_deliver() else None)

        def worker() -> None:
            try:
                result = task()
            except Exception as exc:
                if on_error is not None:
                    deliver(lambda error=exc: on_error(error))
                if on_finally is not None:
                    deliver(on_finally)
                with self._lock:
                    self._handles.pop(task_id, None)
                return

            if on_success is not None:
                deliver(lambda value=result: on_success(value))
            if on_finally is not None:
                deliver(on_finally)
            with self._lock:
                self._handles.pop(task_id, None)

        thread = threading.Thread(target=worker, daemon=True, name=f"scope-task-{task_id[:8]}")
        handle = TaskHandle(task_id, cancel_event, thread)
        with self._lock:
            if self._closed:
                raise RuntimeError("后台任务执行器已经关闭。")
            self._handles[task_id] = handle
            if task_key is not None:
                previous_id = self._latest_by_key.get(task_key)
                if previous_id in self._handles:
                    self._handles[previous_id].cancel()
                self._latest_by_key[task_key] = task_id
        thread.start()
        return handle

    def shutdown(self, timeout_s: float = 2.0) -> None:
        with self._lock:
            self._closed = True
            handles = tuple(self._handles.values())
        for handle in handles:
            handle.cancel()
        for handle in handles:
            handle.wait(timeout_s / max(len(handles), 1))
        while True:
            try:
                self._ui_queue.get_nowait()
            except Empty:
                break

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
