"""Schleifen-Vermeidung.

Ein bidirektionaler Abgleich ohne Schutz läuft im Kreis: Wir schreiben nach Outlook,
lesen die Änderung beim nächsten Lauf als „fremd" zurück, schreiben sie nach TANSS,
lesen sie dort wieder — und so weiter.

Drei Mechanismen greifen ineinander. Der wichtigste ist der erste, und er hat einen
Haken, an dem naive Umsetzungen scheitern.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..domain.identity import SyncKey
from ..state.records import LinkRecord


class EchoGuard:
    """Erkennt das Echo des eigenen Schreibvorgangs.

    **Der Hash stammt immer aus der Serverantwort, nie aus dem gesendeten Payload.**
    TANSS normalisiert beim Speichern: Rundungsregeln verändern die Dauer, und eine
    Zusage wandelt eine Vormerkung in einen festen Termin. Der Zustand nach dem
    Speichern ist also ein *anderer* als der gesendete. Ein Hash über den Payload
    passte nie auf das, was beim nächsten Lauf zurückkommt — jede eigene Schreibung
    käme als fremde Änderung wieder, und der Abgleich liefe endlos.
    """

    def __init__(self, suppression_seconds: int = 30) -> None:
        self.window = timedelta(seconds=suppression_seconds)

    def is_echo(self, link: LinkRecord, side: str, content_hash: str) -> bool:
        stored = link.last_hash_tanss if side == "tanss" else link.last_hash_graph
        return bool(stored) and stored == content_hash

    def in_suppression_window(self, link: LinkRecord, side: str) -> bool:
        """Kurz nach einem eigenen Schreibvorgang ist die Gegenseite noch nicht ruhig."""
        if link.last_written_side != side or link.last_written_at is None:
            return False
        return datetime.now(UTC) - link.last_written_at < self.window

    def mark_written(self, link: LinkRecord, side: str, content_hash: str) -> None:
        if side == "tanss":
            link.last_hash_tanss = content_hash
        else:
            link.last_hash_graph = content_hash
        link.last_written_side = side
        link.last_written_at = datetime.now(UTC)

    def should_skip(self, link: LinkRecord, side: str,
                    content_hash: str) -> tuple[bool, str]:
        if self.is_echo(link, side, content_hash):
            return True, "unverändert seit dem letzten eigenen Schreibvorgang"
        if self.in_suppression_window(link, side):
            return True, "innerhalb des Sperrfensters nach eigenem Schreibvorgang"
        return False, ""


def key_of(link: LinkRecord) -> SyncKey:
    return link.key
