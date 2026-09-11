"""Löschschutz.

Ein bidirektionaler Abgleich ist die klassische Quelle für Massenlöschungen: ein leeres
Ergebnis, ein abgelaufenes Token, ein entfernter Benutzer — und plötzlich hält der
Abgleich hunderte Termine für gelöscht.

Jede Löschung muss durch :meth:`DeletionGuard.authorize`. Es gibt genau eine solche
Stelle, damit sie prüfbar bleibt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..config.models import SafetyConfig
from ..domain.change import SyncAction, SyncOperation
from ..domain.identity import SyncDirection, UserMapping
from ..state.records import LinkRecord

log = logging.getLogger(__name__)

Proof = Literal["tanss_404", "graph_404", "source_removed", "tanss_moved"]
Trigger = Literal["poll_gap", "tanss_event", "graph_delta_removed",
                  "travel_time_removed", "employee_moved", "explicit_cli"]


@dataclass(frozen=True, slots=True)
class DeletionEvidence:
    """Der Nachweis, dass eine Löschung berechtigt ist.

    Für **propagierte** Löschungen ist das immer ein 404 auf das Einzelobjekt. Zieht das
    Werkzeug dagegen eine **eigene Projektion** zurück — einen Fahrt-Termin, weil die
    Fahrtzeit in TANSS auf 0 steht, oder einen Eintrag im alten Postfach nach einem
    Mitarbeiterwechsel —, ist das keine Propagation und braucht keinen 404. Es läuft
    aber unverändert durch Wächter, Sicherung und Protokoll.
    """

    proof: Proof
    probed_at: datetime
    object_id: str
    trigger: Trigger
    http_status: int | None = None
    source_support_id: int | None = None
    new_employee_id: int | None = None


@dataclass(frozen=True, slots=True)
class Authorization:
    allowed: bool
    reason: str


@dataclass(slots=True)
class BatchVerdict:
    allowed: bool
    reason: str
    counted: int = 0
    threshold: int = 0


class DeletionGuard:
    def __init__(self, policy: SafetyConfig, state, *,
                 own_domains: set[str] | None = None) -> None:
        self.policy = policy
        self.state = state
        self.own_domains = own_domains or set()

    # ------------------------------------------------------------------ einzeln

    def authorize(self, action: SyncAction, evidence: DeletionEvidence,
                  link: LinkRecord) -> Authorization:
        """Feste Matrix aus Nachweistyp und Zielseite. Alles andere fällt durch."""
        target = action.target_side.value

        # Unbekannte Schreibrichtung -> ablehnen. Eine stille Annahme waere hier
        # genau der Fehler, der den Schutz aushebelt.
        if link.write_direction not in tuple(SyncDirection):
            return Authorization(False, "unbekannte Schreibrichtung an der Verknüpfung")

        if target == "tanss":
            if link.is_write_protected_in_tanss:
                return Authorization(
                    False,
                    "die Verknüpfung ist TANSS-seitig schreibgeschützt "
                    "(Abwesenheit oder Fahrt-Termin)")
            if link.travel_role != "main":
                return Authorization(
                    False,
                    "Fahrt-Zeilen teilen sich die Support-ID des Haupttermins — "
                    "ihr Löschen darf ihn nie treffen")
            if not (evidence.proof == "graph_404" and evidence.http_status == 404):
                return Authorization(
                    False,
                    f"für eine Löschung in TANSS ist ein 404 in Graph nötig, "
                    f"vorliegt: {evidence.proof}")
            return Authorization(True, "Outlook-Termin nachweislich entfernt")

        # Zielseite Graph
        if evidence.proof == "tanss_404" and evidence.http_status == 404:
            return Authorization(True, "TANSS-Termin nachweislich entfernt")
        if evidence.proof == "source_removed" and link.travel_role != "main":
            return Authorization(True, "Fahrtzeit in TANSS auf 0 — eigene Projektion")
        if evidence.proof == "tanss_moved" and evidence.trigger == "employee_moved":
            if evidence.new_employee_id == link.tanss_employee_id:
                return Authorization(False, "kein Mitarbeiterwechsel erkennbar")
            return Authorization(True, "Termin wurde auf einen anderen Mitarbeiter "
                                       "umgeschoben")

        return Authorization(False, f"kein gültiger Nachweis ({evidence.proof})")

    def authorize_move(self, appointment, link: LinkRecord) -> Authorization:
        """Sonderfall Verschiebung: Darf der Eintrag im alten Postfach weg?

        Ist der abgebende Mitarbeiter **Organisator** einer Besprechung mit externen
        Teilnehmern, dann nicht: Ein Löschen auf dem Organisator-Postfach verschickt
        eine Absage an alle Kunden — und die Kopie beim neuen Mitarbeiter entsteht ohne
        Teilnehmer, ist für den Kunden also unsichtbar. Es gäbe keinen Ersatz für das,
        was der Kunde verliert. Dann wird nur entkoppelt, die Übergabe bleibt Handarbeit.
        """
        if appointment.is_organizer and appointment.external_attendees(self.own_domains):
            return Authorization(
                False,
                "Organisator einer Besprechung mit externen Teilnehmern — "
                "nur entkoppeln, sonst geht eine Absage an die Kunden")
        return Authorization(True, "keine externen Teilnehmer betroffen")

    # ------------------------------------------------------------------ Stapel

    def _alarm_beenden(self, scope: str, counted: int) -> None:
        """Ein Not-Aus erlischt, sobald sein Anlass vorbei ist.

        Freigegeben wurde bisher nur im Zweig ``allow_bulk`` — also nur, wenn ein Lauf
        die Grenze erneut überschreitet. Stehen die Mengenbremsen wieder auf ihrem
        Auslieferungswert 0, wird dieser Zweig nie mehr erreicht, und der Alarm bleibt
        für immer stehen: ``status`` und ``doctor`` melden ihn bei jedem Aufruf. Ein
        Alarm, der immer schrillt, wird abgeschaltet und schützt dann gar nichts mehr.

        Nur ``bulk_delete``: Dass in diesem Lauf nicht zu viel gelöscht wurde, sagt über
        einen Not-Aus wegen zu vieler **Neuanlagen** nichts.
        """
        stop = self.state.active_emergency(scope)
        if stop is None or stop.kind != "bulk_delete":
            return
        log.info("Not-Aus für %s erloschen: %d Löschvorgänge, wieder innerhalb der "
                 "Grenzen", scope, counted)
        self.state.release_emergency(stop.id, by="wieder im Rahmen")

    def check_batch(self, actions: list[SyncAction], linked_total: int, *,
                    scope: str, allow_bulk: bool = False,
                    run_id: int | None = None) -> BatchVerdict:
        """Not-Aus bei Massenlöschungen — gezählt nach **logischen Vorgängen**.

        Abwesenheiten erzeugen einen Datensatz **pro Kalendertag**. Ein zurückgezogener
        Dreiwochenurlaub sind damit 15 Löschungen. Würde nach Einzelterminen gezählt,
        löste jede normale Urlaubsstornierung den Not-Aus aus und der Dienst bliebe
        stehen.
        """
        deletions = [a for a in actions if a.operation is SyncOperation.DELETE]
        if not deletions:
            self._alarm_beenden(scope, 0)
            return BatchVerdict(True, "")

        counted = self._count_operations(deletions)
        ratio = counted / linked_total if linked_total else 0.0

        over_count = (self.policy.max_deletes_per_run > 0
                      and counted > self.policy.max_deletes_per_run)

        # Der Anteilswert braucht eine Untergrenze, sonst ist er bei wenigen
        # Verknuepfungen bedeutungslos: Bei zwei gekoppelten Terminen sind zwei
        # Loeschungen zwangslaeufig 100 % - und ein Testbenutzer mit einer einzigen
        # Kopplung loest bei jeder Loeschung aus. Ein Anteil sagt erst dann etwas,
        # wenn genug Datensaetze da sind, von denen er ein Anteil sein kann.
        ratio_applies = (self.policy.max_delete_ratio > 0
                         and linked_total >= self.policy.ratio_floor)
        over_ratio = ratio_applies and ratio > self.policy.max_delete_ratio
        if not (over_count or over_ratio):
            self._alarm_beenden(scope, counted)
            return BatchVerdict(True, "", counted, self.policy.max_deletes_per_run)

        reason = (f"{counted} Löschvorgänge ({len(deletions)} Termine)"
                  + (f", das sind {ratio:.0%} der Verknüpfungen" if linked_total else ""))

        if allow_bulk:
            log.warning("Not-Aus übersteuert: %s", reason)
            stop = self.state.active_emergency(scope)
            if stop:
                self.state.release_emergency(stop.id, by="--allow-bulk-delete")
            return BatchVerdict(True, f"ausdrücklich freigegeben — {reason}",
                                counted, self.policy.max_deletes_per_run)

        self.state.raise_emergency(
            scope=scope, kind="bulk_delete", counted=counted,
            threshold=self.policy.max_deletes_per_run, reason=reason, run_id=run_id)
        return BatchVerdict(False, reason, counted, self.policy.max_deletes_per_run)

    @staticmethod
    def _count_operations(deletions: list[SyncAction]) -> int:
        """Gruppiert nach Urlaubsantrag bzw. Fahrtgruppe."""
        groups: set[str] = set()
        for action in deletions:
            appt = action.appointment
            if appt.vacation_request_id:
                groups.add(f"vac:{appt.vacation_request_id}")
            elif appt.tanss_support_id:
                groups.add(f"sup:{appt.tanss_support_id}")
            else:
                groups.add(f"key:{appt.key}")
        return len(groups)

    # ------------------------------------------------------------------ Karenz

    def schedule(self, action: SyncAction, evidence: DeletionEvidence,
                 link: LinkRecord) -> None:
        """Terminiert statt sofort auszuführen.

        Beim Wandeln einer Terminvormerkung in einen festen Termin löscht TANSS den
        alten Datensatz und legt Sekunden später einen neuen an — ein echtes
        Löschereignis für einen Termin, der weiterlebt. Das Karenzfenster fängt das ab.
        """
        due = datetime.now(UTC) + timedelta(seconds=self.policy.deletion_grace_seconds)
        self.state.connect().execute(
            "INSERT INTO pending_deletions (scheduled_at, due_at, mailbox, uid, "
            "sequence, travel_role, side, evidence) VALUES (?,?,?,?,?,?,?,?)",
            (int(datetime.now(UTC).timestamp()), int(due.timestamp()),
             link.mailbox, link.uid, link.sequence, link.travel_role,
             action.target_side.value,
             json.dumps({"proof": evidence.proof, "trigger": evidence.trigger,
                         "object_id": evidence.object_id}, ensure_ascii=False)),
        )

    # ------------------------------------------------------------------ Sicherung

    def snapshot_before_delete(self, appointment, side: str, trigger: str) -> int:
        """Nichts wird gelöscht, ohne vorher gesichert zu werden.

        Gesichert wird **alles, was zum Wiederanlegen nötig ist** — nicht nur, was zum
        Wiedererkennen reicht. Eine Sicherung, aus der sich der Termin nicht
        rekonstruieren lässt, ist ein Protokolleintrag und kein Sicherungsnetz.
        """
        payload = {
            "subject": appointment.subject,
            "body": appointment.body,
            "location": appointment.location,
            "start": appointment.start.isoformat() if appointment.start else None,
            "end": appointment.end.isoformat() if appointment.end else None,
            "kind": str(appointment.kind),
            "company_id": appointment.company_id,
            "ticket_id": appointment.ticket_id,
            "attendees": [a.email for a in appointment.attendees],
            # Fuer die Wiederherstellung in TANSS
            "employee_id": appointment.employee_id,
            "duration_minutes": appointment.duration_minutes,
            "service_location": str(appointment.service_location),
            "is_internal": appointment.is_internal,
            "support_type_id": appointment.support_type_id,
            "show_as": appointment.show_as,
            "uid": appointment.key.uid,
            "sequence": appointment.key.sequence,
            "origin": appointment.origin,
            "teams_url": appointment.teams_url,
        }
        cur = self.state.connect().execute(
            "INSERT INTO deleted_backup (deleted_at, side, trigger, mailbox, uid, "
            "sequence, travel_role, tanss_support_id, graph_event_id, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(datetime.now(UTC).timestamp()), side, trigger,
             appointment.key.mailbox, appointment.key.uid, appointment.key.sequence,
             appointment.key.travel_role, appointment.tanss_support_id,
             appointment.graph_event_id,
             json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ Benutzer

    def on_user_retired(self, mapping: UserMapping) -> None:
        """Ein entfernter Benutzer löscht **niemals** Termine.

        Die Rückgabe ist bewusst ``None`` — diese Methode kann per Signatur gar keine
        Löschaktion erzeugen. Damit ist die Invariante nicht nur eine Absprache,
        sondern vom Typsystem getragen.
        """
        log.info("Benutzer %s stillgelegt. Termine bleiben in beiden Systemen "
                 "vollständig bestehen.", mapping.tanss_employee_id)
