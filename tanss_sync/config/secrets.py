"""Verweise auf Geheimnisse — Tokens, Secrets, Zertifikate.

Geheimnisse stehen nie in der Konfiguration, nur Verweise darauf. Eine Form davon
muss **beschreibbar** sein: Der Dienst erneuert sein TANSS-Token selbst und muss das
Ergebnis ablegen können. Mit ``env:`` wäre die Rotation still wirkungslos — eine
Umgebungsvariable kann ein Prozess nicht persistent zurückschreiben.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..util.dateien import atomar_ersetzen

_TOKEN_MODE = 0o600  # der Dienst schreibt selbst hinein


class SecretError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecretRef:
    """``file:/pfad`` | ``env:NAME`` | ``keyring:dienst/schluessel``.

    ``plain:`` gibt es bewusst nicht — Geheimnisse gehören nicht in die Konfiguration.
    """

    raw: str

    @property
    def scheme(self) -> str:
        scheme, _, _ = self.raw.partition(":")
        return scheme.lower()

    @property
    def target(self) -> str:
        _, _, target = self.raw.partition(":")
        return target

    def is_writable(self) -> bool:
        """Nur ``file:`` und ``keyring:`` tragen die Token-Selbstrotation."""
        return self.scheme in ("file", "keyring")

    # ---------------------------------------------------------------- lesen

    def resolve(self) -> str:
        match self.scheme:
            case "file":
                path = Path(self.target).expanduser()
                if not path.exists():
                    raise SecretError(f"Datei nicht gefunden: {path}")
                return path.read_text(encoding="utf-8").strip()
            case "env":
                value = os.environ.get(self.target)
                if not value:
                    raise SecretError(f"Umgebungsvariable {self.target} ist nicht gesetzt")
                return value.strip()
            case "keyring":
                service, _, key = self.target.partition("/")
                try:
                    import keyring  # optional
                except ImportError as exc:  # pragma: no cover
                    raise SecretError("keyring ist nicht installiert") from exc
                value = keyring.get_password(service, key)
                if not value:
                    raise SecretError(f"Kein Eintrag im Schlüsselbund: {self.target}")
                return value.strip()
            case _:
                raise SecretError(
                    f"Unbekannte Verweisform {self.raw!r}. "
                    "Erlaubt sind file:, env: und keyring:."
                )

    # ---------------------------------------------------------------- schreiben

    def write(self, value: str) -> None:
        """Legt einen neuen Wert ab — atomar, mit Vorgängerversion als ``.bak``.

        Atomar heißt: in eine Nachbardatei schreiben, ``fsync``, dann umbenennen.
        Ein abgebrochener Schreibvorgang darf keine halbe Datei hinterlassen — sonst
        wäre das Token nach einem Stromausfall unbrauchbar und der Dienst käme ohne
        Handarbeit nicht mehr hoch.
        """
        if not self.is_writable():
            raise SecretError(
                f"{self.raw!r} ist nicht beschreibbar. Die Token-Erneuerung braucht "
                "file: oder keyring: — mit env: liefe das Token irgendwann ab."
            )
        if self.scheme == "keyring":
            import keyring

            service, _, key = self.target.partition("/")
            keyring.set_password(service, key, value)
            return

        path = Path(self.target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Die Sicherung geht denselben Weg wie das Token selbst. Frueher war sie ein
        # write_bytes() auf einen Pfad: Es folgte einem untergeschobenen Symlink,
        # bekam nie Rechte gesetzt - eine Kopie des Tokens lag mit den Rechten der
        # Umask daneben - und gehoerte nach einem root-Lauf root, woran die naechste
        # Erneuerung als Dienstbenutzer still scheiterte.
        if path.exists():
            alt_inhalt = path.read_text(encoding="utf-8")
            atomar_ersetzen(path.with_suffix(path.suffix + ".bak"),
                            lambda h: h.write(alt_inhalt),
                            standard_mode=_TOKEN_MODE)

        atomar_ersetzen(path, lambda h: h.write(value.strip() + "\n"),
                        standard_mode=_TOKEN_MODE)

    # ---------------------------------------------------------------- pruefen

    def check_permissions(self) -> list[str]:
        """Abweichungen von den erwarteten Dateirechten — für ``doctor``."""
        if self.scheme != "file":
            return []
        path = Path(self.target).expanduser()
        if not path.exists():
            return [f"{path}: fehlt"]
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return [f"{path}: Rechte {mode:04o}, erwartet {_TOKEN_MODE:04o} "
                    "(fremde Benutzer dürfen das Token nicht lesen)"]
        return []
