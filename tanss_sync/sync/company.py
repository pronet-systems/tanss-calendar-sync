"""Welcher Firma ein aus Outlook stammender Termin zugeordnet wird.

In TANSS hängt jeder Termin an einem Kundendatensatz; in Outlook gibt es das nicht. Die
einzige belastbare Spur ist die Maildomäne der **externen** Teilnehmer: Wer von
``@kunde.de`` eingeladen ist, macht den Termin zu einem Termin mit diesem Kunden.

Zwei Eigenheiten der Serverseite bestimmen den Aufbau:

* ``companyId`` ist beim Schreiben Pflicht. Ohne sie lehnt TANSS mit
  ``COMPANY_MUST_BE_GIVEN`` ab — es sei denn, die eigene Firma tritt an ihre Stelle.
  Deshalb gibt es hier **immer** ein Ergebnis, notfalls die eigene Firma.
* Die Suche ist ein Netzaufruf je Domäne. Ein Kalender enthält dieselben Kunden
  dutzendfach, deshalb wird jede Domäne nur einmal nachgeschlagen — auch der Fehlschlag,
  denn sonst fragt jeder Lauf erneut nach denselben Domänen, die es in TANSS nicht gibt.
"""

from __future__ import annotations

import logging

from ..domain.appointment import Appointment
from ..tanss.repository import TanssRepository

log = logging.getLogger(__name__)


class CompanyResolver:
    def __init__(self, tanss: TanssRepository, own_domains: set[str],
                 own_company_id: int = 0) -> None:
        self.tanss = tanss
        self.own_domains = {d.lower() for d in own_domains}
        self.own_company_id = own_company_id
        self._by_domain: dict[str, int | None] = {}

    def resolve(self, appointment: Appointment) -> int:
        """Firmen-Kennung für diesen Termin. Ohne externe Teilnehmer die eigene Firma."""
        for attendee in appointment.external_attendees(self.own_domains):
            found = self._lookup(attendee.domain)
            if found:
                return found
        return self.own_company_id

    def _lookup(self, domain: str) -> int | None:
        if not domain:
            return None
        if domain in self._by_domain:
            return self._by_domain[domain]

        try:
            company = self.tanss.find_company_by_domain(domain)
        except Exception as exc:  # noqa: BLE001
            # Ein Suchfehler darf den Abgleich nicht anhalten. Er wird NICHT
            # zwischengespeichert - beim naechsten Lauf wird es erneut versucht.
            log.warning("Firmensuche für %s fehlgeschlagen: %s", domain, exc)
            return None

        found = company.id if company else None
        if found is None:
            log.info("Keine TANSS-Firma zur Domäne %s — der Termin läuft auf die "
                     "eigene Firma", domain)
        self._by_domain[domain] = found
        return found
