"""Laden und Speichern der Konfiguration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..util.dateien import atomar_ersetzen
from .models import AppConfig

CONFIG_MODE = 0o600
_PARTIAL_SUFFIX = ".partial.json"

DEFAULT_PATHS = (
    Path("./config.json"),
    Path("~/.config/tanss-calendar-sync/config.json").expanduser(),
    Path("/etc/tanss-calendar-sync/config.json"),
)


class ConfigError(RuntimeError):
    pass


class ConfigStore:
    """Findet, lädt und schreibt ``config.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    @classmethod
    def discover(cls, explicit: str | Path | None = None) -> ConfigStore:
        """Erste vorhandene Konfiguration, sonst der bevorzugte Zielpfad."""
        if explicit:
            return cls(Path(explicit))
        for candidate in DEFAULT_PATHS:
            if candidate.exists():
                return cls(candidate)
        return cls(DEFAULT_PATHS[-1])

    # ---------------------------------------------------------------- lesen

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> AppConfig:
        if not self.exists():
            raise ConfigError(
                f"Keine Konfiguration unter {self.path}. "
                "Einrichtung starten mit: tanss-sync setup"
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{self.path} ist kein gültiges JSON: Zeile {exc.lineno}, Spalte {exc.colno} "
                f"— {exc.msg}"
            ) from exc
        try:
            return AppConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{self.path} ist unvollständig oder fehlerhaft:\n"
                              f"{_format_errors(exc)}") from exc

    # ---------------------------------------------------------------- schreiben

    def save(self, config: AppConfig) -> None:
        """Atomar schreiben und Rechte setzen.

        Die Konfiguration enthält zwar keine Geheimnisse, aber die Verweise darauf —
        sie gehört trotzdem niemandem außer dem Dienst.
        """
        self._write(self.path, config.model_dump(mode="json", exclude_none=True))

    @property
    def partial_path(self) -> Path:
        return self.path.with_name(self.path.stem + _PARTIAL_SUFFIX)

    def save_partial(self, data: dict) -> None:
        """Zwischenstand des Einrichtungsassistenten — ein Abbruch verliert nichts."""
        self._write(self.partial_path, data)

    def load_partial(self) -> dict | None:
        if not self.partial_path.exists():
            return None
        try:
            return json.loads(self.partial_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def clear_partial(self) -> None:
        self.partial_path.unlink(missing_ok=True)

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        def schreiben(handle) -> None:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        atomar_ersetzen(path, schreiben, standard_mode=CONFIG_MODE)


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(Wurzel)"
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)
