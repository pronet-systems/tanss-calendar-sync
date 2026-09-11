"""Paarbildung und Vergleich.

Systemunabhängig — der Reconciler kennt nur ``Appointment`` und ``LinkRecord``. Das
macht ihn ohne laufende Systeme testbar, und es ist der Grund, warum die gesamte
Feldkenntnis in den Mappern liegt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..domain.appointment import Appointment
from ..domain.change import ChangeSet, SyncAction, SyncOperation
from ..domain.identity import SyncDirection, SyncKey, UserMapping
from ..domain.uid import uid_matches
from ..state.records import LinkRecord
from .compare import Changed, fields_to_write, what_changed
from .echo import EchoGuard
from .rules import SyncRules

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Pair:
    """Ein Termin auf beiden Seiten — je Seite möglicherweise ``None``."""

    tanss: Appointment | None = None
    graph: Appointment | None = None
    link: LinkRecord | None = None

    @property
    def key(self) -> SyncKey:
        for candidate in (self.tanss, self.graph):
            if candidate is not None:
                return candidate.key
        return self.link.key  # type: ignore[union-attr]


@dataclass(slots=True)
class ReconcileResult:
    changes: ChangeSet = field(default_factory=ChangeSet)
    pairs: list[Pair] = field(default_factory=list)


class Reconciler:
    def __init__(self, rules: SyncRules, echo: EchoGuard) -> None:
        self.rules = rules
        self.echo = echo

    # ------------------------------------------------------------------ Paare

    def pair(self, tanss: list[Appointment], graph: list[Appointment],
             links: list[LinkRecord]) -> list[Pair]:
        """Dreistufig, absichtlich redundant.

        1. Über die gespeicherte Verknüpfung — der schnelle Normalfall.
        2. Über die kanonische UID. Das ist der Wiederherstellungspfad: Auch ohne
           lokale Datenbank lässt sich der Zustand aus beiden Systemen rekonstruieren.
        3. Über unsere Kennung am Outlook-Termin.

        Verglichen wird **nie** mit ``==``, sondern mit :func:`uid_matches` — TANSS
        vergleicht lange UIDs nur über Präfix und Suffix.
        """
        by_key: dict[SyncKey, Pair] = {}
        links_by_key = {link.key: link for link in links}

        for appointment in tanss:
            pair = by_key.setdefault(appointment.key, Pair())
            pair.tanss = appointment
            pair.link = links_by_key.get(appointment.key)

        for appointment in graph:
            match = self._find_partner(appointment, by_key)
            if match is not None:
                match.graph = appointment
                continue
            pair = by_key.setdefault(appointment.key, Pair())
            pair.graph = appointment
            pair.link = pair.link or links_by_key.get(appointment.key)

        # Verknuepfungen ohne Gegenstueck auf beiden Seiten - moegliche Loeschungen.
        for key, link in links_by_key.items():
            if key not in by_key:
                by_key[key] = Pair(link=link)

        return list(by_key.values())

    @staticmethod
    def _find_partner(appointment: Appointment,
                      by_key: dict[SyncKey, Pair]) -> Pair | None:
        exact = by_key.get(appointment.key)
        if exact is not None:
            return exact
        for key, pair in by_key.items():
            if (key.sequence == appointment.key.sequence
                    and key.travel_role == appointment.key.travel_role
                    and key.mailbox == appointment.key.mailbox
                    and uid_matches(key.uid, appointment.key.uid)):
                return pair
        return None

    # ------------------------------------------------------------------ Abgleich

    def reconcile_to_outlook(self, pairs: list[Pair], user: UserMapping, *,
                             created_filtered: bool) -> ChangeSet:
        """Richtung TANSS → Microsoft 365.

        Phase 2 schreibt nur in diese Richtung. Der Rückweg kommt in Phase 3; die
        Paarbildung oben trägt beide bereits.
        """
        changes = ChangeSet()

        for pair in pairs:
            source = pair.tanss
            if source is None:
                # Nur in Outlook vorhanden - das behandelt die Rueckrichtung.
                continue

            verdict = self.rules.should_sync_to_outlook(
                source, user, created_filtered=created_filtered)
            if not verdict:
                changes.skipped.append((source, verdict.reason))
                continue

            if pair.graph is None:
                changes.actions.append(SyncAction(
                    direction=SyncDirection.TANSS_TO_M365,
                    operation=SyncOperation.CREATE,
                    appointment=source,
                    reason="in TANSS vorhanden, in Outlook nicht",
                ))
                continue

            # Hat sich TANSS seit dem letzten Abgleich geaendert? Verglichen wird die
            # TANSS-Seite mit ihrer EIGENEN Ausgangsmarke - nie mit der Graph-Seite.
            state = what_changed(source, pair.graph, pair.link)

            if state.side is Changed.UNKNOWN:
                # Noch keine Ausgangsmarke. Nicht schreiben, sondern den Ist-Zustand
                # als Ausgangspunkt uebernehmen - sonst schriebe der erste Lauf jeden
                # Bestandstermin einmal neu, ohne dass sich etwas geaendert haette.
                changes.baselines.append((pair, state))
                changes.skipped.append(
                    (source, "erster Abgleich dieses Termins — Ausgangsstand übernommen"))
                continue

            if state.side in (Changed.NEITHER, Changed.GRAPH):
                # NEITHER: nichts zu tun. GRAPH: betrifft die Rueckrichtung (Phase 3).
                continue

            if pair.link is not None:
                skip, reason = self.echo.should_skip(pair.link, "graph",
                                                     state.tanss_hash)
                if skip:
                    changes.skipped.append((source, reason))
                    continue

            fields = fields_to_write(source, pair.graph)
            if not fields:
                # In TANSS hat sich etwas geaendert, das Outlook gar nicht fuehrt -
                # nur die Ausgangsmarke nachziehen.
                changes.baselines.append((pair, state))
                continue

            reason = ("in TANSS geändert: " + ", ".join(sorted(fields)))
            if state.is_conflict:
                reason = ("beide Seiten geändert — " + self.rules.config.conflict_winner
                          + " gewinnt: " + ", ".join(sorted(fields)))

            merged = _carry_identity(source, pair.graph)
            changes.actions.append(SyncAction(
                direction=SyncDirection.TANSS_TO_M365,
                operation=SyncOperation.UPDATE,
                appointment=merged,
                reason=reason,
                changed_fields=fields,
            ))

        return changes


def _carry_identity(source: Appointment, target: Appointment) -> Appointment:
    """Übernimmt die Graph-Identität in den TANSS-Stand, damit das Update sein Ziel kennt."""
    source.graph_event_id = target.graph_event_id
    source.series_master_id = source.series_master_id or target.series_master_id
    return source
