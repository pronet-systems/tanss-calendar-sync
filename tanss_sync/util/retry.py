"""Wiederholversuche und Rate-Begrenzung.

Microsoft Graph drosselt pro Postfach **und** App: 10.000 Anfragen je 10 Minuten und
vier gleichzeitige. ``$batch`` umgeht das nicht — jede Teilanfrage zählt einzeln.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableError(RuntimeError):
    """Fehler, bei dem ein erneuter Versuch sinnvoll ist."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(slots=True)
class RetryPolicy:
    """Exponentielles Zurückweichen mit Streuung.

    Wiederholt **nur** 429 und 5xx. Ein 4xx wiederholt sich nicht — es würde beim
    zweiten Versuch genauso scheitern und nur Last erzeugen. Schreiboperationen ohne
    Idempotenzschlüssel werden ebenfalls nicht wiederholt: Ein Timeout heißt nicht,
    dass der Server nichts getan hat.
    """

    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0

    def run(self, func: Callable[[], T], *, scope: str = "") -> T:
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return func()
            except RetryableError as exc:
                last = exc
                if attempt == self.attempts:
                    break
                delay = exc.retry_after if exc.retry_after else self._backoff(attempt)
                log.warning("%s: HTTP %s, Versuch %s/%s — warte %.1fs",
                            scope or "Aufruf", exc.status, attempt, self.attempts, delay)
                time.sleep(delay)
        raise last if last else RuntimeError("Wiederholung ohne Fehler beendet")

    def _backoff(self, attempt: int) -> float:
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (0.5 + random.random() / 2)  # Streuung gegen Gleichtakt


class RateLimiter:
    """Begrenzt die gleichzeitigen Anfragen je Bereich (typisch: je Postfach)."""

    def __init__(self, max_concurrent: int = 4) -> None:
        self._max = max_concurrent
        self._locks: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(max_concurrent))
        self._guard = threading.Lock()

    @contextmanager
    def acquire(self, scope: str):
        with self._guard:
            sem = self._locks[scope]
        sem.acquire()
        try:
            yield
        finally:
            sem.release()


def retry_after_seconds(headers: dict) -> float | None:
    """Liest ``Retry-After``. Der Server weiß besser, wann er wieder mag."""
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
