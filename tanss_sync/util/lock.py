"""Prozess-Sperre für jeden schreibenden Befehl.

Ohne sie arbeiten ``sync --once`` und der laufende Dienst gleichzeitig am selben
Termin. Rein lesende Befehle brauchen sie nicht.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class LockBusy(RuntimeError):
    pass


@dataclass(slots=True)
class LockInfo:
    pid: int
    since: datetime
    command: str


class ProcessLock:
    """Lockdatei mit PID und Startzeit. Erkennt verwaiste Sperren."""

    def __init__(self, path: str | Path, command: str = "") -> None:
        self.path = Path(path).expanduser()
        self.command = command
        self._acquired = False

    def read(self) -> LockInfo | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return LockInfo(int(data["pid"]),
                            datetime.fromisoformat(data["since"]),
                            data.get("command", ""))
        except (ValueError, KeyError, OSError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # existiert, gehoert nur jemand anderem
        except OSError:
            return False
        return True

    def acquire(self) -> ProcessLock:
        existing = self.read()
        if existing and self._alive(existing.pid):
            raise LockBusy(
                f"Ein anderer Vorgang läuft bereits: PID {existing.pid} "
                f"({existing.command or 'unbekannt'}), seit {existing.since:%H:%M:%S}. "
                "Schreibende Befehle laufen nie parallel."
            )
        if existing:
            # Verwaiste Sperre - der Prozess lebt nicht mehr.
            self.path.unlink(missing_ok=True)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "pid": os.getpid(),
            "since": datetime.now(UTC).isoformat(),
            "command": self.command,
        }), encoding="utf-8")
        self._acquired = True
        return self

    def release(self) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> ProcessLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
