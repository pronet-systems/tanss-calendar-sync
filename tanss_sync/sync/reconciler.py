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
from ..domain.uid import is_pending, uid_matches
from ..state.records import LinkRecord
from .adoption import find_adoption
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
    def __init__(self, rules: SyncRules, echo: EchoGuard, clock=None) -> None:
        self.rules = rules
        self.echo = echo
        # Entscheidet bei Abwesenheiten, was "derselbe Kalendertag" heisst.
        self.to_local = clock.local if clock is not None else None

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
        # Die Termin-Kennung ist der belastbarste Wiedererkennungsweg: Sie ueberlebt
        # eine Aenderung der UID, und fuer Fahrt-Termine ist sie der EINZIGE. Ein
        # Fahrt-Block traegt in Outlook eine eigene UID, im Schluessel aber die des
        # Haupttermins - ohne diesen Schritt fiele er nie auf sein Gegenstueck zurueck
        # und wuerde bei jedem Lauf neu angelegt.
        links_by_event = {link.graph_event_id: link for link in links
                          if link.graph_event_id}

        for appointment in tanss:
            pair = by_key.setdefault(appointment.key, Pair())
            pair.tanss = appointment
            pair.link = links_by_key.get(appointment.key)

        for appointment in graph:
            known = links_by_event.get(appointment.graph_event_id)
            if known is not None:
                appointment.key = known.key

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

        # Outlook-Termine ohne Partner - moegliche Uebernahmekandidaten.
        claimed: set[str] = set()
        free_candidates = [p.graph for p in pairs
                           if p.graph is not None and p.tanss is None]

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
                # Ein Termin, der bereits eine Kopplungs-UID traegt, wird NIE
                # angelegt. Die UID sagt: Er war schon einmal in Outlook. Dass wir
                # den Partner nicht sehen, heisst nicht, dass es ihn nicht gibt -
                # er kann eine Serien-Occurrence sein, ausserhalb des Fensters
                # liegen oder von einer Regel ausgenommen sein. Am Kundensystem
                # nachgewiesen: ein Serientermin, den der singleInstance-Filter
                # ausblendet, waere hier ein zweites Mal angelegt worden.
                # Wurde er dagegen wirklich in Outlook geloescht, gehoert das dem
                # Loeschpfad - nicht dem Anlagepfad.
                if source.is_occurrence:
                    # Eine einzelne Occurrence laesst sich in Outlook nicht anlegen -
                    # dort gibt es sie nur als Teil ihrer Serie. Wer es doch versucht,
                    # erzeugt fuer jeden Serientermin einen losen Einzeltermin: genau
                    # die Duplikat-Lawine, die der Serienabgleich verhindern soll.
                    changes.skipped.append((
                        source,
                        "Serientermin ohne Gegenstück in Outlook — eine einzelne "
                        "Occurrence wird nicht angelegt"))
                    continue

                if not is_pending(source.key.uid):
                    changes.skipped.append((
                        source,
                        "bereits gekoppelt, Gegenstück derzeit nicht sichtbar — "
                        "nicht angelegt, um kein Duplikat zu erzeugen"))
                    continue

                # Bevor etwas angelegt wird: Gibt es in Outlook laengst ein
                # Gegenstueck ohne Kopplung? Eine fruehere Terminsynchronisation
                # hinterlaesst genau das - besonders bei Abwesenheiten.
                adoption = find_adoption(
                    source,
                    [c for c in free_candidates if c.graph_event_id not in claimed],
                    to_local=self.to_local)
                if adoption is not None:
                    source.graph_event_id = adoption.candidate.graph_event_id
                    claimed.add(adoption.candidate.graph_event_id)
                    changes.actions.append(SyncAction(
                        direction=SyncDirection.TANSS_TO_M365,
                        operation=SyncOperation.LINK,
                        appointment=source,
                        reason=adoption.reason,
                    ))
                    continue

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
                # Bei einem Konflikt schreibt nur der Gewinner. Schriebe hier auch
                # die Verliererseite, ueberschrieben sich beide Richtungen
                # abwechselnd - der Termin flaeckerte bei jedem Lauf.
                if self.rules.config.conflict_winner != "tanss":
                    changes.skipped.append(
                        (source, "beide Seiten geändert — Outlook gewinnt"))
                    continue
                reason = ("beide Seiten geändert — TANSS gewinnt: "
                          + ", ".join(sorted(fields)))

            merged = _carry_identity(source, pair.graph)
            changes.actions.append(SyncAction(
                direction=SyncDirection.TANSS_TO_M365,
                operation=SyncOperation.UPDATE,
                appointment=merged,
                reason=reason,
                changed_fields=fields,
            ))

        return changes


    # ------------------------------------------------------------------ Löschungen

    def deletion_candidates(self, pairs: list[Pair], user: UserMapping) -> ChangeSet:
        """Termine, bei denen eine Löschung **möglich** ist — nicht erwiesen.

        Hier entsteht ausdrücklich nur ein Verdacht. Dass ein Termin in der Liste fehlt,
        heißt nicht, dass er gelöscht wurde: Er kann aus dem Zeitfenster gewandert sein,
        von einem Filter erfasst werden oder in einer unvollständigen Antwort fehlen.
        Den Nachweis holt der Durchlauf anschließend am Einzelobjekt ein; erst ein 404
        auf gezielte Nachfrage zählt.

        Vorausgesetzt wird immer eine **bestehende Kopplung**: Ohne sie gab es nie ein
        Gegenstück, das verschwunden sein könnte.
        """
        changes = ChangeSet()

        for pair in pairs:
            link = pair.link
            if link is None or link.state != "linked":
                continue

            if pair.tanss is None and pair.graph is not None:
                # In TANSS nicht mehr zu sehen - Verdacht auf Loeschung dort.
                if not user.direction.allows_to_m365():
                    continue
                changes.actions.append(SyncAction(
                    direction=SyncDirection.TANSS_TO_M365,
                    operation=SyncOperation.DELETE,
                    appointment=pair.graph,
                    reason="in TANSS nicht mehr aufgefunden — Nachweis ausstehend",
                ))
                continue

            if pair.graph is None and pair.tanss is not None:
                # Umgekehrt. Der Waechter lehnt das bei schreibgeschuetzten
                # Kopplungen ohnehin ab - hier gar nicht erst vorschlagen, damit
                # eine geloeschte Abwesenheit nie in die Naehe des Loeschpfads kommt.
                if link.travel_role != "main" and not self.rules.config.travel_time_as_separate_events:
                    # Die Projektion wurde abgeschaltet. Dann fehlt jede Fahrt-Zeile
                    # in TANSS - das ist eine Einstellungsaenderung und kein Beleg,
                    # dass die Fahrtzeit entfernt wurde. Bestehende Bloecke bleiben.
                    changes.skipped.append((
                        pair.tanss,
                        "Fahrt-Termine sind abgeschaltet — bestehende Blöcke bleiben "
                        "unangetastet"))
                    continue
                if link.is_write_protected_in_tanss or link.travel_role != "main":
                    continue
                if not user.direction.allows_to_tanss():
                    continue
                changes.actions.append(SyncAction(
                    direction=SyncDirection.M365_TO_TANSS,
                    operation=SyncOperation.DELETE,
                    appointment=pair.tanss,
                    reason="in Outlook nicht mehr aufgefunden — Nachweis ausstehend",
                ))

        return changes


    # ------------------------------------------------------------------ Rückweg

    def reconcile_to_tanss(self, pairs: list[Pair], user: UserMapping) -> ChangeSet:
        """Richtung Microsoft 365 → TANSS.

        Der Rückweg ist der gefährlichere: In TANSS hängen an einem Termin Leistungen,
        Tickets und Abwesenheitsanträge. Ein zu Unrecht geschriebener oder gelöschter
        Datensatz kostet dort mehr als ein überflüssiger Kalendereintrag.

        Drei Sperren, die es in der Gegenrichtung so nicht gibt:

        * **Schreibschutz der Kopplung.** Fahrt-Termine und Abwesenheiten tragen
          ``write_direction = tanss_to_m365``. Ohne diese Prüfung löschte ein in Outlook
          entfernter Urlaubstag den echten Urlaub in TANSS, und ein gelöschter
          Fahrt-Block über die gemeinsame Support-Kennung den Haupttermin.
        * **Entkoppeln statt löschen.** Wird ein gekoppelter Termin unsynchronisierbar —
          auf *Frei* gesetzt, ganztägig gemacht, abgesagt —, endet die Kopplung. Der
          TANSS-Datensatz bleibt. Sonst wäre „auf Frei setzen" ein stiller Löschbefehl.
        * **Kein Anlegen bei bestehender Kopplung.** Wie in der Gegenrichtung: Dass wir
          das TANSS-Gegenstück nicht sehen, heißt nicht, dass es fehlt.
        """
        changes = ChangeSet()

        for pair in pairs:
            source = pair.graph
            if source is None:
                # Nur in TANSS vorhanden - das behandelt die Hinrichtung.
                continue

            if pair.link is not None and (pair.link.is_write_protected_in_tanss
                                          or pair.link.travel_role != "main"):
                # Die Fahrt-Rolle wird getrennt geprueft, obwohl sie den Schreibschutz
                # bereits nach sich zieht. Eine Fahrt-Zeile teilt sich die Support-ID
                # mit dem Haupttermin - eine Aenderung an ihr traefe immer ihn.
                changes.skipped.append(
                    (source, "TANSS-seitig schreibgeschützt (Abwesenheit oder Fahrt)"))
                continue

            if pair.link is not None:
                excluded = self.rules.newly_excluded(source, pair.link)
                if excluded:
                    changes.actions.append(SyncAction(
                        direction=SyncDirection.M365_TO_TANSS,
                        operation=SyncOperation.DETACH,
                        appointment=source,
                        reason=excluded.reason,
                    ))
                    continue

            verdict = self.rules.should_sync_to_tanss(source, user)
            if not verdict:
                changes.skipped.append((source, verdict.reason))
                continue

            if pair.tanss is None:
                if pair.link is not None and pair.link.tanss_support_id:
                    changes.skipped.append((
                        source,
                        "bereits gekoppelt, TANSS-Gegenstück derzeit nicht sichtbar — "
                        "nicht angelegt, um kein Duplikat zu erzeugen"))
                    continue

                owned = self.rules.already_owned_elsewhere(source)
                if owned:
                    changes.skipped.append((source, owned.reason))
                    continue

                if source.is_occurrence:
                    # Umgekehrt dasselbe: In TANSS haengt eine Occurrence an einer
                    # Regel und hat nicht einmal eine eigene Kennung. Sie als
                    # Einzeltermin zu schreiben, loeste sie aus ihrer Serie.
                    changes.skipped.append((
                        source,
                        "Serientermin ohne Gegenstück in TANSS — eine einzelne "
                        "Occurrence wird nicht angelegt"))
                    continue

                changes.actions.append(SyncAction(
                    direction=SyncDirection.M365_TO_TANSS,
                    operation=SyncOperation.CREATE,
                    appointment=source,
                    reason="in Outlook vorhanden, in TANSS nicht",
                ))
                continue

            if pair.tanss.is_occurrence:
                # Eine virtuelle Occurrence traegt in TANSS die Kennung 0 - es gibt
                # keinen Datensatz, den ein Schreibvorgang treffen koennte. TANSS
                # materialisiert erst beim Aendern eine Ausnahme, und das ueber diese
                # Schnittstelle anzustossen ist nicht vorgesehen.
                changes.skipped.append((
                    source,
                    "Serientermin: In TANSS existiert dafür kein eigener Datensatz"))
                continue

            # Hat sich die Graph-Seite gegenueber IHRER eigenen Ausgangsmarke geaendert?
            state = what_changed(pair.tanss, source, pair.link)

            if state.side is Changed.UNKNOWN:
                changes.baselines.append((pair, state))
                changes.skipped.append(
                    (source, "erster Abgleich dieses Termins — Ausgangsstand übernommen"))
                continue

            if state.side in (Changed.NEITHER, Changed.TANSS):
                # NEITHER: nichts zu tun. TANSS: betrifft die Hinrichtung.
                continue

            if state.is_conflict and self.rules.config.conflict_winner != "outlook":
                changes.skipped.append(
                    (source, "beide Seiten geändert — TANSS gewinnt"))
                continue

            if pair.link is not None:
                skip, reason = self.echo.should_skip(pair.link, "tanss",
                                                     state.graph_hash)
                if skip:
                    changes.skipped.append((source, reason))
                    continue

            fields = fields_to_write(source, pair.tanss)
            if not fields:
                changes.baselines.append((pair, state))
                continue

            reason = "in Outlook geändert: " + ", ".join(sorted(fields))
            if state.is_conflict:
                reason = ("beide Seiten geändert — Outlook gewinnt: "
                          + ", ".join(sorted(fields)))

            merged = _carry_tanss_identity(source, pair.tanss)
            changes.actions.append(SyncAction(
                direction=SyncDirection.M365_TO_TANSS,
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


def _carry_tanss_identity(source: Appointment, target: Appointment) -> Appointment:
    """Übernimmt die TANSS-Identität in den Outlook-Stand.

    Ohne ``tanss_support_id`` wüsste das Update nicht, welchen Datensatz es ändert; ohne
    ``employee_id`` und Firmenzuordnung verlöre der Termin beim Schreiben seine
    Zugehörigkeit — ``metaInfos`` und Firma werden in TANSS ersetzt, nicht ergänzt.
    """
    source.tanss_support_id = target.tanss_support_id
    source.employee_id = source.employee_id or target.employee_id
    source.company_id = source.company_id or target.company_id
    source.ticket_id = source.ticket_id or target.ticket_id
    source.recurrence_rule_id = source.recurrence_rule_id or target.recurrence_rule_id
    return source
