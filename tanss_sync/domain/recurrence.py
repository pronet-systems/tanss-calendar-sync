"""Terminserien — systemneutrale Darstellung.

TANSS speichert eine echte RFC-5545-RRULE, Graph ein strukturiertes Objekt. Die
Umrechnung liegt in ``util/rrule.py``; hier steht nur das gemeinsame Modell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class RecurrencePattern:
    rrule: str | None = None  # RFC 5545, TANSS-Seite
    graph: dict | None = None  # strukturiertes recurrence-Objekt
    start_date: datetime | None = None
    end_date: datetime | None = None
    excluded: list[datetime] = field(default_factory=list)

    @property
    def is_endless(self) -> bool:
        """Graph erlaubt endlose Serien, TANSS lehnt sie ab.

        Beim Übertragen muss deshalb ein Enddatum gesetzt werden — siehe
        ``sync.infinite_series_end_years``.
        """
        return self.end_date is None
