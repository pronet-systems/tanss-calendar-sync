"""Fachliche Operationen gegen Microsoft Graph."""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime

from ..domain.uid import canonical_uid
from ..util.timezone import TimeConverter
from .client import GraphClient, GraphNotFound, GraphResyncRequired
from .models import TANSS_ID_PROPERTY, DeltaResult, GraphEvent, GraphUser

log = logging.getLogger(__name__)


class SeriesUidCache:
    """``seriesMasterId`` → kanonische UID.

    Spart je Occurrence einen Zusatzabruf. Eine Serie mit 100 Terminen bräuchte sonst
    100 zusätzliche Anfragen je Durchlauf.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}

    def get(self, mailbox: str, master_id: str) -> str | None:
        return self._cache.get((mailbox, master_id))

    def put(self, mailbox: str, master_id: str, uid: str) -> None:
        self._cache[(mailbox, master_id)] = uid

    def clear(self) -> None:
        self._cache.clear()


class GraphRepository:
    def __init__(self, client: GraphClient, *, timezone: str = "UTC",
                 uid_cache: SeriesUidCache | None = None) -> None:
        self.client = client
        self.time = TimeConverter(timezone)
        self.uid_cache = uid_cache or SeriesUidCache()

    # ------------------------------------------------------------------ Benutzer

    def resolve_user(self, email: str) -> GraphUser | None:
        """Postfach über Mail, UPN oder Alias. Braucht ``User.Read.All``."""
        safe = email.replace("'", "''")
        params = {
            "$filter": f"mail eq '{safe}' or userPrincipalName eq '{safe}'",
            "$select": "id,displayName,mail,userPrincipalName,proxyAddresses,"
                       "givenName,surname,accountEnabled",
            "$top": "5",
        }
        found = list(self.client.paged("/users", params=params, scope="directory"))
        if found:
            return GraphUser.model_validate(found[0])

        # Aliasse stehen nicht in mail/UPN - proxyAddresses braucht einen eigenen Filter.
        params = {
            "$filter": f"proxyAddresses/any(p:p eq 'smtp:{safe}')",
            "$select": "id,displayName,mail,userPrincipalName,proxyAddresses,"
                       "givenName,surname,accountEnabled",
            "$top": "5",
        }
        found = list(self.client.paged("/users", params=params, scope="directory"))
        return GraphUser.model_validate(found[0]) if found else None

    # ------------------------------------------------------------------ Lesen

    def list_calendar_view(self, mailbox: str, start: datetime,
                           end: datetime) -> list[GraphEvent]:
        """Termine im Zeitfenster, Serien bereits expandiert.

        Ohne ``$select``: ``createdDateTime`` und ``lastModifiedDateTime`` unterstützen
        es in ``calendarView`` nicht — genau die beiden Felder, die für den
        Aktivierungsstichtag und die Konfliktauflösung gebraucht werden.
        """
        params = {
            "startDateTime": self.time.graph_window(start),
            "endDateTime": self.time.graph_window(end),
            "$top": "100",
        }
        raw = self.client.paged(f"/users/{_q(mailbox)}/calendarView",
                                params=params, scope=mailbox)
        return [GraphEvent.model_validate(item) for item in raw]

    def delta(self, mailbox: str, *, deltalink: str | None,
              start: datetime, end: datetime) -> DeltaResult:
        """Inkrementelle Änderungen.

        Ohne ``deltalink`` beginnt ein neuer Zyklus mit dem angegebenen Fenster. Mit
        ``deltalink`` wird dieser **unverändert** weiterverwendet — Start und Ende
        stecken darin und lassen sich nicht nachträglich verschieben. Ein gleitendes
        Fenster ist damit nicht möglich; es bewegt sich nur durch eine Neubasierung.
        """
        result = DeltaResult()
        try:
            if deltalink:
                payload = self.client.get_absolute(deltalink, scope=mailbox)
            else:
                params = {
                    "startDateTime": self.time.graph_window(start),
                    "endDateTime": self.time.graph_window(end),
                }
                payload = self.client.get(f"/users/{_q(mailbox)}/calendarView/delta",
                                          params=params, scope=mailbox)
        except GraphResyncRequired:
            result.resync_required = True
            return result

        while True:
            for item in payload.get("value", []):
                if "@removed" in item:
                    if item.get("id"):
                        result.removed_ids.append(item["id"])
                else:
                    result.changed.append(GraphEvent.model_validate(item))
            nxt = payload.get("@odata.nextLink")
            if nxt:
                payload = self.client.get_absolute(nxt, scope=mailbox)
                continue
            result.deltalink = payload.get("@odata.deltaLink")
            return result

    def get_event(self, mailbox: str, event_id: str, *,
                  with_tanss_id: bool = True) -> GraphEvent | None:
        """Einzelner Termin — ``None`` bei 404.

        Dieses ``None`` ist der Löschnachweis: Ein ``@removed`` aus dem Delta allein
        genügt nicht, weil es auch für bloß verschobene Termine erscheint.
        """
        params = {}
        if with_tanss_id:
            params["$expand"] = (
                f"singleValueExtendedProperties($filter=id eq '{TANSS_ID_PROPERTY}')")
        try:
            payload = self.client.get(f"/users/{_q(mailbox)}/events/{_q(event_id)}",
                                      params=params or None, scope=mailbox)
        except GraphNotFound:
            return None
        return GraphEvent.model_validate(payload)

    def find_by_tanss_id(self, mailbox: str, support_id: int) -> GraphEvent | None:
        """Rückwärtssuche über unsere Kennung.

        Der einzige filterbare Weg — Open Extensions unterstützen ``$filter`` nicht.
        """
        prop = TANSS_ID_PROPERTY.replace("'", "''")
        params = {
            "$filter": (f"singleValueExtendedProperties/any(ep: ep/id eq '{prop}' "
                        f"and ep/value eq '{support_id}')"),
            "$top": "5",
        }
        found = list(self.client.paged(f"/users/{_q(mailbox)}/events",
                                       params=params, scope=mailbox))
        return GraphEvent.model_validate(found[0]) if found else None

    # ------------------------------------------------------------------ UID

    def uid_for(self, mailbox: str, event: GraphEvent) -> str:
        """Die kanonische UID eines Termins.

        Für Occurrences und Ausnahmen **nicht** aus der eigenen ``iCalUId``: Die ist bei
        jeder Occurrence eine andere, und ein ``seriesMaster`` erscheint nie im
        ``calendarView``. Wer die UID aus der Occurrence bildet, findet die TANSS-Serie
        nie und legt bei jedem Lauf Duplikate an.

        Stattdessen kommt sie vom Master — einmal geholt, dann zwischengespeichert.
        """
        if not event.is_series_part or not event.series_master_id:
            return canonical_uid(event.ical_uid)

        cached = self.uid_cache.get(mailbox, event.series_master_id)
        if cached:
            return cached

        master = self.get_event(mailbox, event.series_master_id, with_tanss_id=False)
        uid = canonical_uid(master.ical_uid) if master else canonical_uid(event.ical_uid)
        if master is None:
            log.warning("Serien-Master %s in %s nicht auffindbar — nutze die UID der "
                        "Occurrence als Notbehelf", event.series_master_id, mailbox)
        self.uid_cache.put(mailbox, event.series_master_id, uid)
        return uid

    # ------------------------------------------------------------------ Schreiben

    def create_event(self, mailbox: str, payload: dict,
                     transaction_id: str) -> GraphEvent:
        """Termin anlegen.

        ``transactionId`` verhindert eine Doppelanlage, wenn eine Antwort verloren geht
        und wir es erneut versuchen.
        """
        body = {**payload, "transactionId": transaction_id}
        created = self.client.post(f"/users/{_q(mailbox)}/events", body, scope=mailbox)
        return GraphEvent.model_validate(created)

    def update_event(self, mailbox: str, event_id: str, changes: dict) -> GraphEvent:
        """Termin ändern — nur die tatsächlich geänderten Felder.

        ``attendees`` gehört nur hinein, wenn sich die Teilnehmer wirklich geändert
        haben: Schon das bloße Mitsenden löst Aktualisierungsmails an alle aus.
        """
        updated = self.client.patch(f"/users/{_q(mailbox)}/events/{_q(event_id)}",
                                    changes, scope=mailbox)
        return GraphEvent.model_validate(updated)

    def delete_event(self, mailbox: str, event_id: str, *, is_organizer: bool,
                     has_external_attendees: bool) -> None:
        """Termin löschen.

        Auf dem Organisator-Postfach verschickt Graph dabei eine Absage an alle
        Teilnehmer. Das ist bei einer **echten** Löschung gewolltes Verhalten; bei einer
        Verschiebung wäre es ein Fehler. Der Löschwächter entscheidet das vorher — hier
        wird nur protokolliert, was passieren wird.
        """
        if is_organizer and has_external_attendees:
            log.warning("Lösche %s im Organisator-Postfach %s — Graph verschickt dabei "
                        "eine Absage an externe Teilnehmer", event_id, mailbox)
        self.client.delete(f"/users/{_q(mailbox)}/events/{_q(event_id)}", scope=mailbox)

    def respond_to_event(self, mailbox: str, event_id: str,
                         response: str, *, send_response: bool = False) -> None:
        """Zusagen, absagen oder unter Vorbehalt annehmen.

        ``responseStatus`` ist in Graph **nur lesbar** — eine Zusage lässt sich nicht als
        Feld setzen. ``send_response`` steht bewusst auf ``False``: Wir ziehen nur nach,
        was in TANSS bereits entschieden wurde. Sonst bekäme der Organisator bei jedem
        Abgleich eine Mail.
        """
        if response not in ("accept", "decline", "tentativelyAccept"):
            raise ValueError(f"Unbekannte Antwort: {response}")
        self.client.post(f"/users/{_q(mailbox)}/events/{_q(event_id)}/{response}",
                         {"sendResponse": send_response}, scope=mailbox)

    def set_tanss_id(self, mailbox: str, event_id: str, support_id: int) -> None:
        """Hinterlegt unsere Kennung am Termin — der Weg für die Rückwärtssuche."""
        body = {"singleValueExtendedProperties": [
            {"id": TANSS_ID_PROPERTY, "value": str(support_id)}]}
        self.client.patch(f"/users/{_q(mailbox)}/events/{_q(event_id)}", body,
                          scope=mailbox)


def _q(value: str) -> str:
    """URL-Kodierung für Pfadbestandteile — Event-IDs enthalten ``/`` und ``+``."""
    return urllib.parse.quote(value, safe="")
