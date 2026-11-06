"""Base class for one independently supervised stream."""
from __future__ import annotations

import abc
import logging
import threading
from enum import Enum
from typing import Callable

log = logging.getLogger(__name__)


class StreamState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    WAITING = "waiting"
    RECONNECTING = "reconnecting"
    CONNECTED = "connected"
    ERROR = "error"

    @property
    def idle(self) -> bool:
        return self in (StreamState.STOPPED, StreamState.ERROR)


StateCallback = Callable[[str, StreamState, str], None]
LogCallback = Callable[[str, str], None]


class Backoff:
    """Exponential retry delay: 1, 2, 4, 8, 16, 30, 30, ..."""

    def __init__(self, initial: float = 1.0, maximum: float = 30.0) -> None:
        self.initial = initial
        self.maximum = maximum
        self._next = initial

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self.maximum)
        return delay

    def reset(self) -> None:
        self._next = self.initial


class StreamWorker(abc.ABC):
    """Runs ``_run`` on its own thread.

    Each stream has its own worker, thread and FFmpeg process and shares no
    state with the other stream, so one failing cannot disturb the other.
    Callbacks fire on the worker thread and must be thread-safe.
    """

    def __init__(self, name: str, on_state: StateCallback, on_log: LogCallback) -> None:
        self.name = name
        self.state = StreamState.STOPPED
        self.detail = ""
        self._on_state = on_state
        self._on_log = on_log
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the stream. Returns False if it is already running."""
        with self._lock:
            if self.running:
                if not self._stop_event.is_set():
                    return False  # duplicate-process guard
                # Only the tail of a stop is worth waiting for; this may run on
                # the GUI thread. A stop still in progress rejects the start.
                self._thread.join(0.5)
                if self.running:
                    return False
            self._stop_event = threading.Event()
            self._set_state(StreamState.STARTING, "")
            self._thread = threading.Thread(
                target=self._thread_main, args=(self._stop_event,),
                name=f"stream-{self.name}", daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, wait: bool = False) -> None:
        """Request a stop. The worker thread owns FFmpeg and shuts it down."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if wait:
                thread.join(6)
        elif self.state == StreamState.ERROR:
            self._set_state(StreamState.STOPPED, "")

    def _thread_main(self, stop: threading.Event) -> None:
        try:
            self._run(stop)
        except Exception as exc:  # the thread must never die without reporting
            log.exception("%s worker crashed", self.name)
            self._set_state(StreamState.ERROR, str(exc))
        finally:
            if self.state != StreamState.ERROR or stop.is_set():
                self._set_state(StreamState.STOPPED, "")

    @abc.abstractmethod
    def _run(self, stop: threading.Event) -> None:
        """Supervise the stream until ``stop`` is set or a fatal error occurs."""

    def _set_state(self, state: StreamState, detail: str = "") -> None:
        if state == self.state and detail == self.detail:
            return
        self.state = state
        self.detail = detail
        self._on_state(self.name, state, detail)

    def _log(self, message: str) -> None:
        log.info("[%s] %s", self.name, message)
        self._on_log(self.name, message)
