"""Was nicht ins Protokoll gehört."""

from __future__ import annotations

import hashlib
import re

SENSITIVE_HEADERS = frozenset({"apiToken", "refreshToken", "Authorization",
                               "apitoken", "authorization"})

# Alles, was wie ein JWT aussieht - drei durch Punkte getrennte Base64-Bloecke.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


class Redactor:
    """Entfernt Geheimnisse und — auf Wunsch — personenbezogene Inhalte.

    Termininhalte sind personenbezogene Daten. Mit ``redact_content`` werden Betreff und
    Text durch ihre Prüfsumme ersetzt: Dann bleibt nachvollziehbar, *dass* sich etwas
    geändert hat, ohne dass der Inhalt gespeichert wird. Für Umgebungen mit strengen
    Datenschutzvorgaben ist das der empfohlene Betriebsmodus.
    """

    def __init__(self, *, redact_content: bool = False) -> None:
        self.redact_content = redact_content

    @staticmethod
    def redact_headers(headers: dict[str, str]) -> dict[str, str]:
        return {k: ("<entfernt>" if k in SENSITIVE_HEADERS else v)
                for k, v in headers.items()}

    @staticmethod
    def scrub(text: str | None) -> str:
        """Ersetzt Tokens in beliebigem Text — etwa in Fehlermeldungen."""
        if not text:
            return ""
        cleaned = _JWT_RE.sub("<token>", text)
        return _BEARER_RE.sub(r"\1<token>", cleaned)

    def content(self, value: str | None) -> str | None:
        """Betreff oder Text — im Klartext oder als Prüfsumme."""
        if value is None:
            return None
        if not self.redact_content:
            return value
        if not value:
            return ""
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<sha256:{digest} len={len(value)}>"

    def snapshot(self, data: dict | None) -> dict | None:
        """Bereinigt einen Vorher-/Nachher-Schnappschuss."""
        if data is None:
            return None
        out = {}
        for key, value in data.items():
            if key in ("subject", "body", "location") and isinstance(value, str):
                out[key] = self.content(value)
            elif isinstance(value, str):
                out[key] = self.scrub(value)
            else:
                out[key] = value
        return out
