"""HTTP-Zugriff auf die TANSS-API.

Zwei Pfade mit unterschiedlichen Regeln:

``/api/tanss.x/v1/**``
    Die Integrations-Schnittstelle. Hier setzt der Server die Persist-Optionen, wegen
    derer wir diesen Weg nehmen. **``loggedInUserId`` darf hier niemals angehängt
    werden** — nicht weil der Aufruf scheitern würde, sondern weil der Request dadurch
    zusätzlich als Benutzer gilt und serverseitig andere Zweige nimmt. Er verhält sich
    dann anders als der, den wir getestet haben, und der Unterschied fällt erst im
    Produktivbetrieb auf.

``/api/v1/**``
    Braucht **immer** ``loggedInUserId``; ohne den Parameter antwortet die Route mit 403.
    Die Rechteprüfung greift dann als der genannte Mitarbeiter — wir umgehen keine
    Berechtigungen, sondern nutzen genau dessen.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import (
    ChangesDiscardedError,
    CompanyRequiredError,
    TanssAuthError,
    TanssDuplicateError,
    TanssError,
    TanssNotFound,
)

log = logging.getLogger(__name__)

TANSS_X_PREFIX = "/api/tanss.x/v1"
V1_PREFIX = "/api/v1"


class TanssClient:
    """Reines HTTP. Kennt keine Fachlogik."""

    def __init__(self, base_url: str, auth, *, timeout: int = 30,
                 verify_tls: bool = True, log_http: bool = False,
                 client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.log_http = log_http
        self._http = client or httpx.Client(timeout=timeout, verify=verify_tls)
        self._act_as: int | None = None

    def act_as(self, employee_id: int) -> TanssClient:
        """Sicht, die bei ``/api/v1``-Aufrufen ``loggedInUserId`` anhängt."""
        view = TanssClient.__new__(TanssClient)
        view.base_url = self.base_url
        view.auth = self.auth
        view.log_http = self.log_http
        view._http = self._http
        view._act_as = employee_id
        return view

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TanssClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- Verben

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def put(self, path: str, json_body: Any = None, **params: Any) -> Any:
        return self._request("PUT", path, json_body=json_body, params=params)

    def post(self, path: str, json_body: Any = None, **params: Any) -> Any:
        return self._request("POST", path, json_body=json_body, params=params)

    def delete(self, path: str, **params: Any) -> Any:
        return self._request("DELETE", path, params=params)

    def get_with_meta(self, path: str, **params: Any) -> tuple[Any, dict]:
        """Wie :meth:`get`, liefert zusätzlich den ``meta``-Block (linkedEntities)."""
        return self._request("GET", path, params=params, want_meta=True)

    def put_with_meta(self, path: str, json_body: Any = None,
                      **params: Any) -> tuple[Any, dict]:
        return self._request("PUT", path, json_body=json_body, params=params,
                             want_meta=True)

    # ---------------------------------------------------------------- intern

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 params: dict | None = None, want_meta: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}

        # Ein ausdrueckliches _as_employee schlaegt den act_as-Kontext (fuer /jwts).
        explicit = params.pop("_as_employee", None)
        actor = explicit if explicit is not None else self._act_as

        if path.startswith(V1_PREFIX) and not path.startswith(TANSS_X_PREFIX):
            if actor is None:
                raise TanssAuthError(
                    f"{path} ist eine /api/v1-Route und braucht loggedInUserId. "
                    "Ohne den Parameter antwortet TANSS mit 403. "
                    "Nutze client.act_as(<employeeId>)."
                )
            params["loggedInUserId"] = actor

        url = f"{self.base_url}{path}"
        if self.log_http:
            log.debug("%s %s params=%s", method, path, params)

        try:
            response = self._http.request(method, url, params=params, json=json_body,
                                          headers=self.auth.header())
        except httpx.RequestError as exc:
            raise TanssError(f"TANSS nicht erreichbar: {exc}") from exc

        if self.log_http:
            log.debug("-> %s (%s Bytes)", response.status_code, len(response.content))

        return self._handle(response, want_meta=want_meta)

    def _handle(self, response: httpx.Response, *, want_meta: bool) -> Any:
        status = response.status_code

        if status == 204 or not response.content:
            return ({}, {}) if want_meta else {}

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if 200 <= status < 300:
            if isinstance(payload, dict) and "content" in payload:
                content = payload["content"]
                return (content, payload.get("meta", {})) if want_meta else content
            return (payload, {}) if want_meta else payload

        self._raise(status, payload, response.text)

    @staticmethod
    def _raise(status: int, payload: Any, text: str) -> None:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error") or {}
            detail = (error.get("text") or error.get("localizedText")
                      or payload.get("detail") or "")

        if status == 404 or "OBJECT_NOT_FOUND" in detail:
            raise TanssNotFound("Objekt nicht gefunden", status=status, detail=detail)
        if "CHANGES_WERE_DISCARDED" in detail:
            raise ChangesDiscardedError(
                "Der Termin wurde inzwischen in eine Leistung gewandelt — Kopplung beenden",
                status=status, detail=detail)
        if "COMPANY_MUST_BE_GIVEN" in detail:
            raise CompanyRequiredError("Jeder Termin braucht eine Firma",
                                       status=status, detail=detail)
        if "DUPLICATE" in detail.upper():
            raise TanssDuplicateError("Ein gleichartiger Termin existiert bereits",
                                      status=status, detail=detail)
        if status in (401, 403):
            raise TanssAuthError(
                _auth_hint(detail), status=status, detail=detail)

        raise TanssError(f"TANSS antwortete mit HTTP {status}: {detail or text[:200]}",
                         status=status, detail=detail)


def _auth_hint(detail: str) -> str:
    base = "Zugriff verweigert"
    if detail:
        base = f"{base} ({detail})"
    return (f"{base}. Häufigste Ursachen: fehlendes loggedInUserId bei einer "
            "/api/v1-Route, abgelaufenes Token, oder dem Token-Inhaber fehlt ein Recht.")
