"""Event-Regeln in TANSS — die Meldewege, über die TANSS Änderungen anstößt.

Zwei Aufgaben, die nichts miteinander zu tun haben und trotzdem dieselben Daten lesen:

**Eigene Regeln pflegen.** Nur im Push-Betrieb nötig. Je synchronisiertem Mitarbeiter
genau **eine** Regel, die auf unsere Rückrufadresse zeigt.

**Fremde Regeln erkennen.** Immer nötig, auch im Poll-Betrieb. Eine Regel mit fremder
Zieladresse ist ein harter Beleg dafür, dass ein anderes System denselben Kalender
abgleicht — und zwei Systeme, die gegeneinander schreiben, erzeugen Duplikate. Das
**Fehlen** einer Regel ist umgekehrt kein Beleg für das Gegenteil: Die Richtung
Outlook → TANSS kommt ohne Event-Regel aus.

Zur Bereinigung: Mehrfache Regeln für denselben Mitarbeiter mit derselben Zieladresse
sind Altlasten — jede davon feuert, und jeder Schuss ist ein weiterer Aufruf für
dasselbe Ereignis. Entfernt wird dabei nie die letzte Regel eines Ziels, sondern nur
die Überzähligen.

Eine Eigenheit, die den Entwurf bestimmt: ``active: false`` legt eine Regel **nicht**
still — sie feuert trotzdem. Stilllegen geht ausschließlich über Löschen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..tanss.models import TanssEventRule
from ..tanss.repository import TanssRepository

log = logging.getLogger(__name__)


def service_of(url: str) -> str:
    """Der Dienst hinter einer Rückrufadresse — ohne die Kennung am Ende.

    Jede Regel bekommt beim Anlegen eine eigene Kennung im Pfad. Wer nach der
    vollständigen Adresse gruppiert, findet deshalb **nie** ein Duplikat, obwohl
    drei Regeln denselben Mitarbeiter an denselben Dienst melden. Verglichen wird
    darum alles bis zum letzten Pfadsegment.
    """
    head, _, _ = (url or "").rstrip("/").rpartition("/")
    return head or url or ""


@dataclass(slots=True)
class RuleGroup:
    """Alle Regeln eines Mitarbeiters, die auf denselben Dienst melden."""

    employee_id: int
    url: str
    rules: list[TanssEventRule] = field(default_factory=list)

    @property
    def is_foreign(self) -> bool:
        return all(getattr(rule, "_foreign", False) for rule in self.rules)

    @property
    def duplicates(self) -> list[TanssEventRule]:
        """Die überzähligen. Die älteste — die kleinste Kennung — bleibt."""
        if len(self.rules) < 2:
            return []
        return sorted(self.rules, key=lambda r: r.id)[1:]


@dataclass(slots=True)
class RuleReport:
    own: list[TanssEventRule] = field(default_factory=list)
    foreign: list[TanssEventRule] = field(default_factory=list)
    without_webhook: list[TanssEventRule] = field(default_factory=list)
    groups: list[RuleGroup] = field(default_factory=list)

    @property
    def duplicates(self) -> list[TanssEventRule]:
        return [rule for group in self.groups for rule in group.duplicates]

    @property
    def foreign_employees(self) -> set[int]:
        return {eid for rule in self.foreign for eid in rule.employee_ids}


class WebhookManager:
    def __init__(self, tanss: TanssRepository, own_callback_url: str = "") -> None:
        self.tanss = tanss
        self.own_url = own_callback_url or ""

    # ------------------------------------------------------------------ lesen

    def inspect(self) -> RuleReport:
        """Alle Regeln einlesen und einordnen. Rein lesend."""
        report = RuleReport()
        groups: dict[tuple[int, str], RuleGroup] = {}

        for rule in self.tanss.list_event_rules():
            urls = rule.webhook_urls
            if not urls:
                # Eine Regel ohne Webhook geht uns nichts an - sie macht etwas
                # anderes, etwa eine Mail verschicken.
                report.without_webhook.append(rule)
                continue

            eigen = bool(self.own_url) and all(self._is_ours(url) for url in urls)
            if eigen:
                report.own.append(rule)
            else:
                report.foreign.append(rule)
            # Merkt am Objekt, wem die Regel gehoert - die Bereinigung muss eigene
            # von fremden Regeln unterscheiden koennen.
            object.__setattr__(rule, "_foreign", not eigen)

            for employee_id in rule.employee_ids:
                for service in {service_of(url) for url in urls}:
                    key = (employee_id, service)
                    group = groups.setdefault(key, RuleGroup(employee_id, service))
                    group.rules.append(rule)

        report.groups = [g for g in groups.values() if len(g.rules) > 1]
        return report

    def _is_ours(self, url: str) -> bool:
        return bool(self.own_url) and self.own_url in url

    # ------------------------------------------------------------------ pflegen

    def missing_for(self, employee_ids: list[int], report: RuleReport) -> list[int]:
        """Mitarbeiter ohne eigene Regel — für ``webhooks sync``."""
        covered = {eid for rule in report.own for eid in rule.employee_ids}
        return [eid for eid in employee_ids if eid not in covered]

    def create_for(self, employee_id: int) -> int:
        if not self.own_url:
            raise ValueError(
                "Ohne push.callback_base_url lässt sich keine eigene Regel anlegen — "
                "TANSS wüsste nicht, wohin es melden soll.")
        rule_id = self.tanss.create_event_rule(employee_id, self.own_url)
        log.info("Event-Regel %s für Mitarbeiter %s angelegt", rule_id, employee_id)
        return rule_id

    def remove(self, rule: TanssEventRule) -> None:
        """Löschen ist der **einzige** Weg, eine Regel stillzulegen."""
        self.tanss.delete_event_rule(rule.id)
        log.info("Event-Regel %s entfernt (%s)", rule.id, rule.name or "ohne Namen")
