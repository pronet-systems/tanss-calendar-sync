"""TANSS-Authentifizierung und Token-Selbstrotation.

Das Werkzeug braucht **niemals** Zugangsdaten. Es bekommt einmalig ein Token für eine
externe Anbindung und stellt danach seinen eigenen Nachfolger aus.

Warum das geht: ``/api/v1/jwts/**`` verlangt die Rolle ``USER``, und die bekommt ein
``TANSS_APP``-Token über ``loggedInUserId``. Der reguläre Refresh-Weg scheidet aus — er
verlangt ein JWT mit numerischem ``sub``, unseres trägt ``"TANSS_APP"``.

**Rotation ist keine Invalidierung.** Das abgelöste Token bleibt bis zu seinem Ablauf
gültig; einen Widerruf kennt TANSS 10.10.0 nicht. Wer ein Token wirklich entwerten muss,
kommt um einen Eingriff im TANSS nicht herum.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config.secrets import SecretRef
from .errors import TanssAuthError

log = logging.getLogger(__name__)

EXT_PROGRAM = "tanss_app"  # den Wert "tanss_x" gibt es nicht
_MS_PER_DAY = 86_400_000
_TEST_DURATION_MS = 60_000  # Trockentest: 60 Sekunden


@dataclass(slots=True)
class RotationResult:
    rotated: bool
    reason: str
    old_expires_at: datetime | None = None
    new_expires_at: datetime | None = None
    error: str | None = None


def decode_jwt_claims(token: str) -> dict:
    """Liest die Claims, ohne die Signatur zu prüfen.

    Wir prüfen hier nichts — wir wollen nur wissen, wann das Token abläuft. Die
    Signatur prüft der Server.
    """
    raw = token.removeprefix("Bearer ").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        raise TanssAuthError("Token ist kein JWT (drei durch Punkt getrennte Teile erwartet)")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise TanssAuthError("Token-Inhalt ist nicht lesbar") from exc


class TanssAuth:
    """Hält das Token, kennt seine Restlaufzeit und erneuert es rechtzeitig."""

    def __init__(self, token_ref: SecretRef, *, owner_employee_id: int,
                 duration_days: int = 90) -> None:
        self._ref = token_ref
        self.owner_employee_id = owner_employee_id
        self.duration_days = duration_days
        self._cached: str | None = None

    # ---------------------------------------------------------------- Zugriff

    @property
    def token(self) -> str:
        if self._cached is None:
            self._cached = self._ref.resolve()
        return self._cached

    def header(self) -> dict[str, str]:
        """Der Header heißt ``apiToken``, nicht ``Authorization``.

        Der Wert trägt das Präfix ``Bearer `` bereits — TANSS liefert ihn so aus.
        """
        value = self.token
        if not value.startswith("Bearer "):
            value = f"Bearer {value}"
        return {"apiToken": value}

    def expires_at(self) -> datetime | None:
        try:
            exp = decode_jwt_claims(self.token).get("exp")
        except TanssAuthError:
            return None
        return datetime.fromtimestamp(exp, UTC) if exp else None

    def days_remaining(self) -> int | None:
        expiry = self.expires_at()
        if expiry is None:
            return None
        return (expiry - datetime.now(UTC)).days

    @property
    def subject(self) -> str | None:
        try:
            return decode_jwt_claims(self.token).get("sub")
        except TanssAuthError:
            return None

    # ---------------------------------------------------------------- Rotation

    def mint_token(self, client, *, duration_days: int | None = None,
                   info: str = "tanss-sync", for_testing: bool = False) -> str:
        """Stellt ein neues Token aus.

        ``loggedInUserId`` ist **zwingend** — ohne den Parameter antwortet die Route mit
        403, weil ein ``TANSS_APP``-Token allein die Rolle ``USER`` nicht hat.
        """
        days = duration_days if duration_days is not None else self.duration_days
        duration_ms = _TEST_DURATION_MS if for_testing else days * _MS_PER_DAY
        payload = client.get(
            f"/api/v1/jwts/{EXT_PROGRAM}",
            duration=duration_ms,
            info=info,
            isForTesting="true" if for_testing else "false",
            _as_employee=self.owner_employee_id,
        )
        token = payload.get("apiToken") if isinstance(payload, dict) else None
        if not token:
            raise TanssAuthError("Die Token-Ausstellung lieferte kein Token zurück")
        return token

    def can_rotate(self, client) -> bool:
        """Trockentest der Rotationsfähigkeit.

        Erzeugt mit ``isForTesting=true`` und 60 Sekunden Laufzeit ein Token, das weder
        im Token-Log auftaucht noch brauchbar ist — die Rechteprüfung läuft trotzdem.
        Damit lässt sich vorab feststellen, ob die Rotation später greifen wird, statt
        es erst 60 Tage vor Ablauf zu merken.
        """
        try:
            self.mint_token(client, for_testing=True)
            return True
        except TanssAuthError:
            return False

    def rotate_if_needed(self, client, *, before_days: int = 60,
                         verify) -> RotationResult:
        """Erneuert das Token, wenn die Restlaufzeit unter die Schwelle fällt.

        Das neue Token wird **gegengetestet**, bevor es übernommen wird. Schlägt der
        Test fehl, bleibt das alte aktiv — es ist ja noch gültig. Ein stillschweigender
        Wechsel auf ein unbrauchbares Token wäre der schlechtere Ausgang.
        """
        old_expiry = self.expires_at()
        remaining = self.days_remaining()

        if remaining is None:
            return RotationResult(False, "Restlaufzeit nicht ermittelbar",
                                  old_expires_at=old_expiry)
        if remaining > before_days:
            return RotationResult(False, f"noch {remaining} Tage gültig",
                                  old_expires_at=old_expiry)

        log.info("Token läuft in %s Tagen ab — erneuere", remaining)
        try:
            fresh = self.mint_token(client)
        except TanssAuthError as exc:
            return RotationResult(False, "Ausstellung fehlgeschlagen",
                                  old_expires_at=old_expiry, error=str(exc))

        if not verify(fresh):
            return RotationResult(
                False, "neues Token bestand den Gegentest nicht — altes bleibt aktiv",
                old_expires_at=old_expiry,
                error="Gegentest fehlgeschlagen",
            )

        self._ref.write(fresh)
        self._cached = fresh
        new_expiry = self.expires_at()
        log.info("Token erneuert, gültig bis %s", new_expiry)
        return RotationResult(True, "erneuert und gegengetestet",
                              old_expires_at=old_expiry, new_expires_at=new_expiry)

    def warning(self) -> str | None:
        """Hinweis für ``status`` und ``doctor`` — None, wenn alles in Ordnung ist."""
        remaining = self.days_remaining()
        if remaining is None:
            return "Restlaufzeit des Tokens nicht ermittelbar"
        if remaining < 0:
            return ("Token ist abgelaufen. Selbstrotation ist jetzt unmöglich — ein neues "
                    "Token muss hinterlegt werden: tanss-sync token set")
        if remaining < 30:
            return f"Token läuft in {remaining} Tagen ab"
        return None


def next_rotation(expires_at: datetime | None, before_days: int) -> datetime | None:
    if expires_at is None:
        return None
    return expires_at - timedelta(days=before_days)
