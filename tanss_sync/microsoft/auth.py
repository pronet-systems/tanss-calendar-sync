"""Microsoft-Authentifizierung — App-only über Client Credentials.

Delegierte Berechtigungen scheiden aus: Sie funktionieren nicht für fremde Postfächer,
und ein unbeaufsichtigter Dienst hat niemanden, der sich anmelden könnte.

Zwei Anwendungsberechtigungen sind nötig, nicht eine:

``Calendars.ReadWrite``
    Termine lesen und schreiben. Lässt sich über Exchange-RBAC auf die Zielpostfächer
    beschränken.

``User.Read.All``
    Postfächer zu TANSS-Mitarbeitern auflösen. **Nicht** über Exchange-RBAC
    einschränkbar — gilt verzeichnisweit lesend. Wer alle Postfächer von Hand pflegt und
    die automatische Erkennung abschaltet, kommt ohne sie aus.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config.models import MicrosoftConfig
from ..config.secrets import SecretRef

log = logging.getLogger(__name__)

SCOPE = ["https://graph.microsoft.com/.default"]  # immer .default, nie Einzelrechte


class GraphAuthError(RuntimeError):
    pass


class GraphAuth:
    """Holt und erneuert das App-Token. MSAL übernimmt das Zwischenspeichern."""

    def __init__(self, config: MicrosoftConfig) -> None:
        self.config = config
        self._app = None

    def _client(self):
        if self._app is not None:
            return self._app
        try:
            import msal
        except ImportError as exc:  # pragma: no cover
            raise GraphAuthError("msal ist nicht installiert") from exc

        authority = f"https://login.microsoftonline.com/{self.config.tenant_id}"
        credential = self._credential()
        self._app = msal.ConfidentialClientApplication(
            client_id=self.config.client_id,
            authority=authority,
            client_credential=credential,
        )
        return self._app

    def _credential(self):
        auth = self.config.auth
        if auth.mode == "secret":
            return SecretRef(auth.client_secret_ref).resolve()

        path = Path(auth.certificate_path).expanduser()
        if not path.exists():
            raise GraphAuthError(f"Zertifikat nicht gefunden: {path}")
        return {
            "thumbprint": auth.thumbprint.replace(":", "").replace(" ", "").upper(),
            "private_key": path.read_text(encoding="utf-8"),
        }

    def token(self) -> str:
        result = self._client().acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            raise GraphAuthError(_explain(result))
        return result["access_token"]

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}


def _explain(result: dict) -> str:
    """Macht aus einer Entra-Fehlerantwort einen brauchbaren Hinweis."""
    code = result.get("error", "unbekannt")
    description = (result.get("error_description") or "").split("\r\n")[0]

    hints = {
        "invalid_client": "Client-ID, Secret oder Zertifikat stimmen nicht.",
        "unauthorized_client": "Die App ist im Mandanten nicht zugelassen.",
        "invalid_scope": "Die Berechtigungen fehlen oder wurden nicht administrativ bestätigt.",
        "invalid_request": "Mandanten-ID prüfen.",
    }
    hint = hints.get(code, "")
    return f"Microsoft lehnt die Anmeldung ab ({code}): {description} {hint}".strip()
