"""Fachliche Operationen gegen TANSS.

Der Rest des Programms spricht ausschließlich hiermit — nie direkt mit dem Client.
Hier liegen die Zusicherungen, die in der API leicht zu verletzen sind.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .client import TANSS_X_PREFIX, TanssClient
from .errors import TanssError, TanssNotFound
from .models import (
    TRIGGER_TYPES,
    SupportPage,
    TanssCompany,
    TanssEmployee,
    TanssEventRule,
    TanssSupport,
    TanssSupportWrite,
)

log = logging.getLogger(__name__)

RULE_NAME = "tanss-sync"


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


class TanssRepository:
    def __init__(self, client: TanssClient, own_company_id: int = 0) -> None:
        self.client = client
        self.own_company_id = own_company_id

    # ------------------------------------------------------------------ lesen

    def list_technicians(self) -> list[TanssEmployee]:
        """Alle Techniker samt E-Mail — die Grundlage der Postfach-Zuordnung.

        Bewusst über ``/api/tanss.x/v1``: Die ``/api/v1``-Entsprechung liefert **keine**
        E-Mail-Adresse und wäre für das Mapping wertlos.
        """
        payload = self.client.get(f"{TANSS_X_PREFIX}/technicians") or []
        return [TanssEmployee.model_validate(item) for item in payload]

    def own_state(self, as_employee_id: int) -> dict:
        """Eigener Zustand inklusive ``ownCompanyId`` — spart eine Rückfrage im Setup."""
        return self.client.act_as(as_employee_id).get("/api/v1/employees/ownState") or {}

    def list_appointments(self, employee_id: int, start: datetime, end: datetime, *,
                          with_recurring: bool = False,
                          created_from: datetime | None = None) -> SupportPage:
        """Termine eines Mitarbeiters im Zeitfenster.

        Drei Zusicherungen, die hier und nicht beim Aufrufer liegen:

        * Der Filter heißt ``employees`` (Liste). Ein falscher Name wird stillschweigend
          ignoriert und liefert **alle** Mitarbeiter — wir würden in fremde Postfächer
          schreiben.
        * **Kein** ``planningTypes``-Filter. Sonst verschwindet ein zur Leistung
          gewandelter Termin aus der Antwort und sähe aus wie eine Löschung.
          Aussortiert wird im Client.
        * ``fetchLinkedEmployees`` wird **nie** gesetzt — TANSS liefert dann virtuelle
          Zeilen mit ``id: 0``, die weder gepaart noch geschrieben werden dürfen.

        ``created_from`` bildet den Aktivierungsstichtag ab. Er muss serverseitig
        filtern: ``dateCreated`` steht nicht in der Listenantwort, ein Nachfilter liefe
        also ins Leere und der Erstlauf würde den gesamten Altbestand übertragen.
        """
        body: dict = {
            "timeframe": {"from": _epoch(start), "to": _epoch(end)},
            "employees": [employee_id],
            "fetchMetaInfos": True,
        }
        if with_recurring:
            # fetchRecurring expandiert die Occurrences. fetchRecurrenceRules tut das
            # NICHT - es liefert nur das Regel-Objekt an den Mastern.
            body["fetchRecurring"] = True
        if created_from is not None:
            body["creationTimeframe"] = {"from": _epoch(created_from), "to": 0}

        content, meta = self.client.put_with_meta(f"{TANSS_X_PREFIX}/supports", body)
        items = [TanssSupport.model_validate(item) for item in (content or [])]
        return SupportPage(
            items=items,
            employee_id=employee_id,
            meta_fetched=True,
            recurring_expanded=with_recurring,
            created_from=created_from,
            linked_entities=meta.get("linkedEntities", {}),
        )

    def get_support(self, support_id: int) -> TanssSupport | None:
        """Einzelner Termin — ``None`` **ausschließlich** bei 404.

        Dieses ``None`` ist der Löschnachweis aus Invariante 2, und deshalb darf es auf
        genau einem Weg entstehen: Der Server hat gezielt geantwortet, dass es das
        Objekt nicht gibt. Eine leere Antwort mit Erfolgsstatus ist **kein** solcher
        Nachweis — sie bedeutet, dass etwas anderes schiefgelaufen ist, und wird als
        Fehler gemeldet statt als Löschung ausgelegt.

        Der Einzelabruf liefert außerdem ``outlookReadOnly``, ``modified`` und
        ``dateCreated``, die in der Liste fehlen.
        """
        try:
            payload = self.client.get(f"{TANSS_X_PREFIX}/supports/{support_id}")
        except TanssNotFound:
            return None

        if not payload:
            raise TanssError(
                f"TANSS lieferte für Termin {support_id} eine leere Antwort mit "
                "Erfolgsstatus. Das ist kein Löschnachweis — der Abgleich bricht hier "
                "ab, statt den Termin für entfernt zu halten.")
        return TanssSupport.model_validate(payload)

    def search_companies(self, query: str) -> list[TanssCompany]:
        content = self.client.put(f"{TANSS_X_PREFIX}/search",
                                  {"areas": ["COMPANY"], "query": query}) or {}
        return [TanssCompany.model_validate(c) for c in content.get("companies", [])]

    def find_company_by_domain(self, domain: str) -> TanssCompany | None:
        """Firma über die Maildomain eines externen Teilnehmers."""
        if not domain:
            return None
        for company in self.search_companies(domain):
            haystack = f"{company.email} {company.website}".lower()
            if domain.lower() in haystack:
                return company
        return None

    def list_custom_entries(self, employee_id: int, start: datetime,
                            end: datetime) -> list[dict]:
        """Custom-Einträge über den Zeitstrahl — kein Push nötig."""
        body = {
            "entity": "EMPLOYEE",
            "ids": [employee_id],
            "timeframe": {"from": _epoch(start), "to": _epoch(end)},
        }
        content = self.client.act_as(employee_id).put("/api/v1/timeline", body) or {}
        entries: list[dict] = []
        for info in content.get("infos", []) or []:
            entries.extend(info.get("customEntries", []) or [])
        return entries

    # ------------------------------------------------------------------ schreiben

    def create_support(self, write: TanssSupportWrite, *,
                       prevent_notification: bool) -> TanssSupport:
        """Termin anlegen.

        Der Parameter heißt ``isOrganizer``, wirkt aber als ``preventNotification`` —
        er unterdrückt die TANSS-interne Benachrichtigung. Beim ``PUT`` hat derselbe
        Name eine **andere** Bedeutung; beides darf nicht verwechselt werden.

        Der Endpunkt setzt serverseitig ``useOwnCompanyIfNoneGiven``,
        ``tanssXduplicateChecks``, ``determineTicketId`` und
        ``automaticallyResolveConflicts``.
        """
        payload = self.client.post(
            f"{TANSS_X_PREFIX}/supports", write.payload(),
            isOrganizer="true" if prevent_notification else "false",
        )
        return TanssSupport.model_validate(payload)

    def update_support(self, support_id: int, write: TanssSupportWrite) -> TanssSupport:
        """Termin ändern — immer mit ``discardOnSupport=true``.

        Wurde der Termin inzwischen zur Leistung gewandelt, antwortet TANSS mit
        ``CHANGES_WERE_DISCARDED``. Das ist die Server-Auskunft „nicht mehr dein Objekt"
        und wird als *Kopplung beenden* verarbeitet — eine Serverprüfung statt einer
        Clientvermutung.
        """
        payload = self.client.put(
            f"{TANSS_X_PREFIX}/supports/{support_id}", write.payload(),
            discardOnSupport="true",
        )
        return TanssSupport.model_validate(payload)

    def delete_support(self, support_id: int) -> None:
        self.client.delete(f"{TANSS_X_PREFIX}/supports/{support_id}")

    # ------------------------------------------------------------------ Event-Regeln

    def list_event_rules(self) -> list[TanssEventRule]:
        payload = self.client.put(f"{TANSS_X_PREFIX}/tanssEvents/rules", {}) or []
        return [TanssEventRule.model_validate(rule) for rule in payload]

    def create_event_rule(self, employee_id: int, url: str,
                          name: str = RULE_NAME) -> int:
        body = {
            "name": name,
            "active": True,
            "assignments": [{"linkType": "SUPPORT", "linkId": 0}],
            "employees": [{"employeeId": employee_id}],
            "triggerTypes": [{"triggerType": t} for t in TRIGGER_TYPES],
            "actions": [{"actionType": "WEBHOOK",
                         "params": {"url": url, "method": "POST"}}],
        }
        payload = self.client.post(f"{TANSS_X_PREFIX}/tanssEvents/rules", body)
        return int(payload["id"])

    def delete_event_rule(self, rule_id: int) -> None:
        self.client.delete(f"{TANSS_X_PREFIX}/tanssEvents/rules/{rule_id}")

    def raw_event_rules(self) -> list[dict]:
        """Die Regeln **unverändert**, wie TANSS sie liefert.

        Für die Sicherung: Das Modell lässt Felder weg, die es nicht braucht — für ein
        späteres Wiederanlegen zählt aber jedes. Eine Sicherung, die nur enthält, was
        wir gerade auswerten, stellt nichts wieder her.
        """
        return list(self.client.put(f"{TANSS_X_PREFIX}/tanssEvents/rules", {}) or [])

    def create_event_rule_raw(self, body: dict) -> int:
        """Legt eine Regel aus einer Sicherung neu an.

        Die Kennungen werden entfernt: ``id`` und die ``ruleId`` in den Unterobjekten
        vergibt der Server. Bliebe die alte Kennung stehen, schriebe man entweder in
        eine fremde Regel oder bekäme eine Abweisung.
        """
        payload = {k: v for k, v in body.items() if k != "id"}
        for feld in ("assignments", "employees", "triggerTypes", "actions"):
            eintraege = payload.get(feld) or []
            payload[feld] = [
                {k: v for k, v in eintrag.items() if k not in ("id", "ruleId")}
                for eintrag in eintraege
            ]
        created = self.client.post(f"{TANSS_X_PREFIX}/tanssEvents/rules", payload)
        return int(created["id"])
