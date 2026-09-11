"""Betreffbildung.

In TANSS gehört jeder Termin zu einem Kundendatensatz, in Outlook gibt es das nicht.
Damit dort erkennbar bleibt, zu welchem Kunden ein Termin gehört, hängt der Abgleich
den Firmennamen in runden Klammern an.

Die Klammern sind dabei **Metadaten, kein Text** — vorhandene Klammerinhalte im Betreff
gehen verloren. Das ist das Verhalten des Originalprodukts und ändert sich nicht dadurch,
dass wir es nachbauen; wir dokumentieren es nur ehrlich.
"""

from __future__ import annotations

import re

from ..domain.appointment import Appointment
from ..util.html import HtmlText

_COMPANY_RE = re.compile(r"\s*\((?:Firma|Company):[^)]*\)\s*$")
_ANY_PARENS_RE = re.compile(r"\s*\([^)]*\)")


class SubjectFormatter:
    def __init__(self, *, append_company: bool = True) -> None:
        self.append_company = append_company

    def to_outlook(self, appointment: Appointment, company_name: str | None) -> str:
        """Betreff für den Outlook-Termin.

        ``outlookTitle`` ist bei allem **leer**, was in TANSS entsteht — also genau in
        dieser Richtung. Ohne den Rückfall auf die erste Zeile des Textes bekäme jeder
        daraus erzeugte Termin einen leeren Betreff. Bei einer Abwesenheit steht dort
        etwa „Urlaub", was als Titel genau richtig ist.
        """
        base = appointment.subject.strip() or HtmlText.first_line(appointment.body)
        base = _ANY_PARENS_RE.sub("", base).strip() or "Termin"

        if self.append_company and company_name:
            return f"{base} (Firma: {company_name})"
        return base

    @staticmethod
    def to_tanss(subject: str) -> str:
        """Entfernt das ``(Firma: …)``-Suffix wieder.

        Nur das Suffix am Ende — andere Klammern im Betreff bleiben stehen, damit ein
        aus Outlook stammender Titel nicht unnötig beschnitten wird.
        """
        return _COMPANY_RE.sub("", subject or "").strip()
