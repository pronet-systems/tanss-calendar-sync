"""HTTP-Zugriff auf Microsoft Graph.

Zwei Dinge passieren hier bei **jeder** Anfrage, weil sie sonst irgendwann vergessen
werden:

``Prefer: IdType="ImmutableId"``
    Ohne diesen Header ändern sich Event-IDs, sobald ein Termin in einen anderen Ordner
    wandert. Der Header gilt nur für die Anfrage, in der er gesetzt ist — er muss
    deshalb überall mit.

Drosselung
    10.000 Anfragen je 10 Minuten und vier gleichzeitige, jeweils je Postfach und App.
    ``$batch`` umgeht das nicht; jede Teilanfrage zählt einzeln.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from ..util.retry import RateLimiter, RetryableError, RetryPolicy, retry_after_seconds

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
IMMUTABLE_ID = 'IdType="ImmutableId"'


class GraphError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class GraphNotFound(GraphError):
    """404 ``ErrorItemNotFound`` — im Löschpfad der einzige gültige Nachweis."""


class GraphResyncRequired(GraphError):
    """410 ``resyncRequired`` bzw. ungültiger Delta-Link — Neubasierung nötig."""


class GraphClient:
    def __init__(self, auth, *, timeout: int = 30, max_concurrent: int = 4,
                 log_http: bool = False, client: httpx.Client | None = None) -> None:
        self.auth = auth
        self.log_http = log_http
        self._http = client or httpx.Client(timeout=timeout)
        self._limiter = RateLimiter(max_concurrent)
        self._retry = RetryPolicy()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- Verben

    def get(self, path: str, *, params: dict | None = None,
            scope: str = "") -> dict:
        return self._request("GET", path, params=params, scope=scope)

    def post(self, path: str, body: Any = None, *, scope: str = "") -> dict:
        return self._request("POST", path, body=body, scope=scope)

    def patch(self, path: str, body: Any = None, *, scope: str = "") -> dict:
        return self._request("PATCH", path, body=body, scope=scope)

    def delete(self, path: str, *, scope: str = "") -> None:
        self._request("DELETE", path, scope=scope)

    def get_absolute(self, url: str, *, scope: str = "") -> dict:
        """Folgt einem vollständigen Link — ``nextLink`` oder ``deltaLink``.

        Der Link wird **unverändert** verwendet und nie zerlegt: Bei einem Delta-Link
        stecken die Fensterparameter darin.
        """
        return self._request("GET", url, absolute=True, scope=scope)

    def paged(self, path: str, *, params: dict | None = None,
              scope: str = "") -> Iterator[dict]:
        payload = self.get(path, params=params, scope=scope)
        while True:
            yield from payload.get("value", [])
            nxt = payload.get("@odata.nextLink")
            if not nxt:
                return
            payload = self.get_absolute(nxt, scope=scope)

    # ---------------------------------------------------------------- intern

    def _request(self, method: str, path: str, *, body: Any = None,
                 params: dict | None = None, absolute: bool = False,
                 scope: str = "") -> dict:
        url = path if absolute else f"{GRAPH_BASE}{path}"
        headers = {**self.auth.header(), "Prefer": IMMUTABLE_ID}

        def attempt() -> dict:
            with self._limiter.acquire(scope or "global"):
                try:
                    response = self._http.request(method, url, params=params,
                                                  json=body, headers=headers)
                except httpx.RequestError as exc:
                    raise RetryableError(f"Graph nicht erreichbar: {exc}") from exc
            return self._handle(response)

        if self.log_http:
            log.debug("%s %s", method, path if not absolute else "<link>")
        return self._retry.run(attempt, scope=f"{method} {path[:60]}")

    def _handle(self, response: httpx.Response) -> dict:
        status = response.status_code
        if status == 204 or not response.content:
            return {}

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if 200 <= status < 300:
            return payload

        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = error.get("code", "")
        message = error.get("message", response.text[:200])

        if status in (429, 500, 502, 503, 504):
            raise RetryableError(message, status=status,
                                 retry_after=retry_after_seconds(dict(response.headers)))
        if status == 404 or code in ("ErrorItemNotFound", "itemNotFound"):
            raise GraphNotFound(message, status=status, code=code)
        if status == 410 or code in ("resyncRequired", "syncStateNotFound"):
            raise GraphResyncRequired(
                "Der Delta-Link ist nicht mehr gültig — Neubasierung nötig",
                status=status, code=code)
        if status == 403 and "ErrorAccessDenied" in code:
            raise GraphError(
                f"{message} — die App darf auf dieses Postfach nicht zugreifen. "
                "Prüfen: Anwendungsberechtigung erteilt und administrativ bestätigt? "
                "Exchange-RBAC kann bis zu einer Stunde brauchen, bis es greift.",
                status=status, code=code)

        raise GraphError(f"Graph antwortete mit HTTP {status} ({code}): {message}",
                         status=status, code=code)
