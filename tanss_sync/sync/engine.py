"""Der Abgleich selbst.

Phase 2 schreibt die Richtung TANSS → Microsoft 365. Aufbau und Schutzmechanismen sind
bereits für beide Richtungen ausgelegt.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

from ..audit.logger import AuditLogger
from ..audit.redaction import Redactor
from ..config.models import AppConfig
from ..domain.change import Outcome, SyncAction, SyncOperation
from ..domain.identity import SyncDirection, UserMapping
from ..domain.uid import canonical_uid, pending_uid
from ..microsoft.mapper import GraphMapper
from ..microsoft.repository import GraphRepository
from ..state.records import LinkRecord, RunReport
from ..state.store import StateStore
from ..tanss.errors import ChangesDiscardedError
from ..tanss.mapper import TanssMapper
from ..tanss.repository import TanssRepository
from ..util.html import HtmlText
from ..util.timezone import TimeConverter
from .company import CompanyResolver
from .compare import fingerprint
from .deletion import DeletionEvidence, DeletionGuard
from .echo import EchoGuard
from .reconciler import Reconciler
from .rules import SyncRules
from .subject import SubjectFormatter

log = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, config: AppConfig, tanss: TanssRepository,
                 graph: GraphRepository, state: StateStore) -> None:
        self.config = config
        self.tanss = tanss
        self.graph = graph
        self.state = state

        time = TimeConverter(config.microsoft.timezone)
        html = HtmlText()
        self.tanss_mapper = TanssMapper(time, html=html)
        self.graph_mapper = GraphMapper(time, html=html)
        self.subject = SubjectFormatter(
            append_company=config.sync.company_suffix_in_subject)
        self.company = CompanyResolver(tanss, config.own_mail_domains,
                                       config.tanss.own_company_id)
        self.rules = SyncRules(config.sync)
        self.echo = EchoGuard(config.sync.echo_suppression_seconds)
        self.reconciler = Reconciler(self.rules, self.echo, time)
        self.guard = DeletionGuard(config.safety, state,
                                   own_domains=config.own_mail_domains)
        self.holder = f"{os.getpid()}:{threading.current_thread().name}"
        # Nachweise zu Loeschkandidaten, gueltig nur innerhalb eines Laufs.
        self._evidence: dict[int, DeletionEvidence] = {}

    # ------------------------------------------------------------------ Durchlauf

    def run_once(self, *, dry_run: bool = False, allow_bulk_delete: bool = False,
                 allow_bulk_create: bool = False) -> RunReport:
        # Ein in der Konfiguration gesetztes dry_run gilt IMMER. Es ist die
        # Sicherung, die jemand bewusst gesetzt hat - eine vergessene
        # Kommandozeilenoption darf sie nicht aushebeln.
        dry_run = dry_run or self.config.sync.dry_run
        report = RunReport()
        run_id = self.state.begin_run()
        audit = AuditLogger(self.state, Redactor(
            redact_content=self.config.logging.redact_content), run_id)

        for user in self._active_users():
            try:
                self.sync_user(user, audit, report, dry_run=dry_run,
                               allow_bulk_delete=allow_bulk_delete,
                               allow_bulk_create=allow_bulk_create)
            except Exception as exc:  # ein Benutzer darf den Lauf nicht kippen
                report.errors += 1
                log.exception("Abgleich für %s gescheitert", user.tanss_employee_id)
                report.detail.setdefault("errors", []).append(
                    {"employee": user.tanss_employee_id, "error": str(exc)})

        self.state.finish_run(run_id, report)
        return report

    def sync_user(self, user: UserMapping, audit: AuditLogger, report: RunReport, *,
                  dry_run: bool, allow_bulk_delete: bool,
                  allow_bulk_create: bool = False) -> None:
        scope = f"user:{user.tanss_employee_id}"
        if not self.state.acquire_lease(scope, self.holder,
                                        self.config.sync.lease_ttl_seconds):
            log.info("%s wird gerade von einem anderen Ablauf bearbeitet", scope)
            return
        try:
            self._sync_user_locked(user, audit, report, dry_run=dry_run,
                                   allow_bulk_delete=allow_bulk_delete,
                                   allow_bulk_create=allow_bulk_create)
        finally:
            self.state.release_lease(scope, self.holder)

    # ------------------------------------------------------------------ intern

    def _sync_user_locked(self, user: UserMapping, audit: AuditLogger,
                          report: RunReport, *, dry_run: bool,
                          allow_bulk_delete: bool,
                          allow_bulk_create: bool = False) -> None:
        start, end = self._window()
        scope = f"user:{user.tanss_employee_id}"

        # --- TANSS lesen. Der Aktivierungsstichtag filtert SERVERSEITIG; ein
        # Nachfilter liefe ins Leere, weil dateCreated in der Liste fehlt.
        page = self.tanss.list_appointments(
            user.tanss_employee_id, start, end, created_from=user.activated_at)
        tanss_appointments = self._map_tanss(page, user)

        # --- Graph lesen
        events = self.graph.list_calendar_view(user.mailbox, start, end)
        graph_appointments = [
            self.graph_mapper.to_appointment(
                event, user.mailbox, self.graph.uid_for(user.mailbox, event),
                own_domains=self.config.own_mail_domains)
            for event in events
            # calendarView liefert nie einen seriesMaster; Occurrences und Ausnahmen
            # kommen erst mit Phase 5.
            if event.type == "singleInstance"
        ]

        # Betreff VOR dem Vergleich in die Outlook-Fassung bringen. Danach waere es
        # zu spaet: Der Reconciler haette dann den rohen TANSS-Titel gegen den bereits
        # mit "(Firma: ...)" versehenen Outlook-Titel gehalten - und jeden Termin fuer
        # geaendert gehalten.
        for appointment in tanss_appointments:
            appointment.subject = self.subject.to_outlook(
                appointment, appointment.company_name)

        links = self.state.links_for_user(user.tanss_employee_id)
        pairs = self.reconciler.pair(tanss_appointments, graph_appointments, links)
        changes = self.reconciler.reconcile_to_outlook(
            pairs, user, created_filtered=page.created_from is not None)

        # Der Rueckweg arbeitet auf denselben Paaren. Beide Richtungen koennen
        # denselben Termin betreffen - der Konfliktfall ist in beiden Richtungen
        # so entschieden, dass nur der Gewinner schreibt.
        back = self.reconciler.reconcile_to_tanss(pairs, user)
        changes.actions.extend(back.actions)
        changes.skipped.extend(back.skipped)
        changes.baselines.extend(back.baselines)

        # Loeschungen zuletzt - und nur die, fuer die der Nachweis am Einzelobjekt
        # gelingt. Alles andere faellt hier heraus und wird nie zu einer Aktion.
        for candidate in self.reconciler.deletion_candidates(pairs, user).actions:
            proven, why = self._prove_deletion(candidate, user)
            if proven is None:
                # Der Termin ist noch da. Eine etwa laufende Vormerkung wird
                # zurueckgenommen - genau dafuer gibt es die Karenzzeit.
                if not dry_run:
                    taken_back = self.state.cancel_deletion(
                        candidate.appointment.key, candidate.target_side.value, why)
                    if taken_back:
                        log.info("Vorgemerkte Löschung von %s zurückgenommen: %s",
                                 candidate.appointment.key, why)
                changes.skipped.append((candidate.appointment, why))
                continue
            self._evidence[id(candidate)] = proven
            changes.actions.append(candidate)

        for appointment, reason in changes.skipped:
            report.skipped += 1
            audit.record_skip(appointment, reason)

        # Ausgangsmarken setzen - ohne zu schreiben. Das ist der Grund, warum der
        # erste Lauf gegen einen Bestand ruhig bleibt: Er merkt sich, wie beide
        # Seiten aussehen, statt sie aneinander anzugleichen.
        if not dry_run:
            for pair, state in changes.baselines:
                self._store_baseline(pair, state, user)

        # Uebernahmen sind KEINE Neuanlagen - sie erzeugen nichts, sie verknuepfen nur.
        creates = [a for a in changes.actions if a.operation is SyncOperation.CREATE]
        adoptions = [a for a in changes.actions if a.operation is SyncOperation.LINK]
        if adoptions:
            log.info("%s bestehende Outlook-Termine werden übernommen statt angelegt",
                     len(adoptions))
        if len(creates) > self.config.safety.max_creates_per_run and not allow_bulk_create:
            reason = (f"{len(creates)} Neuanlagen in einem Lauf "
                      f"(Grenze {self.config.safety.max_creates_per_run})")
            report.aborted_reason = f"{scope}: {reason}"
            self.state.raise_emergency(
                scope=scope, kind="bulk_create", counted=len(creates),
                threshold=self.config.safety.max_creates_per_run, reason=reason)
            log.error("Not-Aus für %s: %s. Ein erster Lauf gegen einen gewachsenen "
                      "Kalender sieht so aus — erst mit --dry-run prüfen.", scope, reason)
            return

        verdict = self.guard.check_batch(
            changes.actions, self.state.count_links(user.tanss_employee_id),
            scope=scope, allow_bulk=allow_bulk_delete)
        if not verdict.allowed:
            report.aborted_reason = f"{scope}: {verdict.reason}"
            audit.record_block(verdict, scope=scope)
            log.error("Not-Aus für %s: %s", scope, verdict.reason)
            return

        for action in changes.actions:
            self._apply(action, user, audit, report, dry_run=dry_run)

    def _store_baseline(self, pair, state, user: UserMapping) -> None:
        """Merkt sich den Ist-Zustand beider Seiten, ohne etwas zu verändern."""
        appointment = pair.tanss or pair.graph
        if appointment is None:
            return
        link = pair.link or LinkRecord.for_new(appointment, user)
        link.tanss_support_id = (pair.tanss.tanss_support_id if pair.tanss
                                 else link.tanss_support_id)
        link.graph_event_id = (pair.graph.graph_event_id if pair.graph
                               else link.graph_event_id)
        link.last_hash_tanss = state.tanss_hash or link.last_hash_tanss
        link.last_hash_graph = state.graph_hash or link.last_hash_graph
        link.last_seen_at = datetime.now(UTC)
        self.state.upsert_link(link)

    def _map_tanss(self, page, user: UserMapping) -> list:
        out = []
        for support in page.items:
            uid, sequence = self.tanss_mapper.uid_of(support)
            if not uid:
                # Noch nie gekoppelt - die UID entsteht erst beim Anlegen in Graph.
                uid = pending_uid(support.id)
            appointment = self.tanss_mapper.to_appointment(support, user.mailbox, uid)
            if sequence >= 0:
                appointment.key = appointment.key.__class__(
                    user.mailbox, uid, sequence, "main")
            out.append(appointment)
        return out

    def _apply(self, action: SyncAction, user: UserMapping, audit: AuditLogger,
               report: RunReport, *, dry_run: bool) -> None:
        if dry_run:
            audit.record(action, outcome=Outcome.DRY_RUN)
            _bump(report, action.operation)
            return

        started = datetime.now(UTC)
        try:
            if action.direction is SyncDirection.M365_TO_TANSS:
                if action.operation is SyncOperation.CREATE:
                    self._create_in_tanss(action, user)
                elif action.operation is SyncOperation.UPDATE:
                    self._update_in_tanss(action, user)
                elif action.operation is SyncOperation.DETACH:
                    self._detach(action, user)
                elif action.operation is SyncOperation.DELETE:
                    if not self._delete(action, user):
                        audit.record(action, outcome=Outcome.SCHEDULED)
                        return
                else:
                    return
            elif action.operation is SyncOperation.CREATE:
                self._create_in_graph(action, user)
            elif action.operation is SyncOperation.UPDATE:
                self._update_in_graph(action, user)
            elif action.operation is SyncOperation.LINK:
                self._adopt_in_graph(action, user)
            elif action.operation is SyncOperation.DELETE:
                if not self._delete(action, user):
                    audit.record(action, outcome=Outcome.SCHEDULED)
                    return
            else:
                return
        except Exception as exc:  # noqa: BLE001
            report.errors += 1
            audit.record(action, outcome=Outcome.FAILED, error=str(exc))
            log.warning("%s fehlgeschlagen: %s", action.describe(), exc)
            return

        audit.record(action, outcome=Outcome.OK,
                     duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000))
        _bump(report, action.operation)

    def _create_in_graph(self, action: SyncAction, user: UserMapping) -> None:
        appointment = action.appointment
        payload = self.graph_mapper.to_create_payload(appointment,
                                                      include_attendees=False)
        created = self.graph.create_event(user.mailbox, payload,
                                          transaction_id=str(uuid.uuid4()))

        # Die Kopplungs-UID entsteht ERST hier - vorher gibt es sie nicht.
        uid = canonical_uid(created.ical_uid)
        appointment.graph_event_id = created.id
        appointment.key = appointment.key.__class__(
            user.mailbox, uid, appointment.key.sequence, appointment.key.travel_role)

        if appointment.tanss_support_id:
            try:
                self.graph.set_tanss_id(user.mailbox, created.id,
                                        appointment.tanss_support_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kennung am Termin konnte nicht gesetzt werden: %s", exc)

        link = LinkRecord.for_new(appointment, user)
        link.last_hash_tanss = fingerprint(appointment)
        after = self.graph_mapper.to_appointment(created, user.mailbox, uid)
        link.last_hash_graph = fingerprint(after)
        self.echo.mark_written(link, "graph", link.last_hash_graph)
        self.state.upsert_link(link)

        # Kopplung nach TANSS zurueckschreiben. Scheitert das, ist der Termin in
        # Outlook trotzdem angelegt und verknuepft - der Lauf darf ihn deshalb NICHT
        # als gescheitert melden. Sonst steht im Protokoll "fehlgeschlagen", waehrend
        # in Wahrheit ein Termin entstanden ist: der gefaehrlichste aller Zustaende,
        # weil niemand danach aufraeumt.
        try:
            self._write_back_coupling(appointment)
        except Exception as exc:  # noqa: BLE001
            log.warning("Kopplung für Support %s nicht zurückgeschrieben: %s. "
                        "Der Outlook-Termin ist angelegt und lokal verknüpft.",
                        appointment.tanss_support_id, exc)

    def _adopt_in_graph(self, action: SyncAction, user: UserMapping) -> None:
        """Einen bestehenden Outlook-Termin übernehmen, ohne ihn zu verändern.

        Es wird **nichts** am Termin geschrieben außer der Kennung, an der wir ihn
        später wiederfinden. Der Sinn der Übernahme ist ja gerade, dass er bereits
        richtig ist.
        """
        appointment = action.appointment
        existing = self.graph.get_event(user.mailbox, appointment.graph_event_id,
                                        with_tanss_id=False)
        if existing is None:
            log.warning("Übernahmekandidat %s ist verschwunden — übersprungen",
                        appointment.graph_event_id)
            return

        uid = self.graph.uid_for(user.mailbox, existing)
        appointment.key = appointment.key.__class__(
            user.mailbox, uid, appointment.key.sequence, appointment.key.travel_role)

        if appointment.tanss_support_id:
            try:
                self.graph.set_tanss_id(user.mailbox, existing.id,
                                        appointment.tanss_support_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kennung am übernommenen Termin nicht gesetzt: %s", exc)

        link = LinkRecord.for_new(appointment, user)
        link.graph_event_id = existing.id
        after = self.graph_mapper.to_appointment(existing, user.mailbox, uid)
        link.last_hash_graph = fingerprint(after)
        link.last_hash_tanss = fingerprint(appointment)
        link.last_seen_at = datetime.now(UTC)
        self.state.upsert_link(link)

        try:
            self._write_back_coupling(appointment)
        except Exception as exc:  # noqa: BLE001
            log.warning("Kopplung für Support %s nicht zurückgeschrieben: %s",
                        appointment.tanss_support_id, exc)

    def _update_in_graph(self, action: SyncAction, user: UserMapping) -> None:
        appointment = action.appointment
        existing = self.graph.get_event(user.mailbox, appointment.graph_event_id,
                                        with_tanss_id=False)
        existing_body = (existing.body or {}).get("content") if existing else None

        payload = self.graph_mapper.to_update_payload(
            appointment, action.changed_fields, existing_body=existing_body)
        if not payload:
            return
        self.graph.update_event(user.mailbox, appointment.graph_event_id, payload)

        # Beide Ausgangsmarken aus dem ZURUECKGEMELDETEN Zustand bilden, nicht aus
        # dem gesendeten Payload: Der Server normalisiert, und ein Hash ueber das
        # Gesendete passte nie auf das, was beim naechsten Lauf zurueckkommt.
        written = self.graph.get_event(user.mailbox, appointment.graph_event_id,
                                       with_tanss_id=False)
        link = self.state.get_link(appointment.key) or LinkRecord.for_new(appointment, user)
        if written is not None:
            after = self.graph_mapper.to_appointment(
                written, user.mailbox, appointment.key.uid)
            link.last_hash_graph = fingerprint(after)
        link.last_hash_tanss = fingerprint(appointment)
        self.echo.mark_written(link, "graph", link.last_hash_graph or "")
        link.last_seen_at = datetime.now(UTC)
        self.state.upsert_link(link)

    # ------------------------------------------------------------------ Löschen

    def _prove_deletion(self, action: SyncAction, user: UserMapping):
        """Fragt das vermisste Objekt **gezielt** nach. Nur ein 404 ist ein Nachweis.

        Jede andere Antwort bedeutet: Das Objekt existiert noch, es war nur nicht in der
        Liste. Dann wird nicht gelöscht — und das ist kein Fehler, sondern der Normalfall
        bei einem verschobenen Zeitfenster.
        """
        appointment = action.appointment
        now = datetime.now(UTC)

        if action.direction is SyncDirection.TANSS_TO_M365:
            support_id = appointment.tanss_support_id
            if not support_id:
                link = self.state.get_link(appointment.key)
                support_id = link.tanss_support_id if link else None
            if not support_id:
                return None, "keine TANSS-Kennung zur Nachfrage vorhanden"
            if self.tanss.get_support(support_id) is not None:
                return None, "in TANSS weiterhin vorhanden — keine Löschung"
            return DeletionEvidence(
                proof="tanss_404", probed_at=now, object_id=str(support_id),
                trigger="tanss_deleted", http_status=404), ""

        event_id = appointment.graph_event_id
        if not event_id:
            link = self.state.get_link(appointment.key)
            event_id = link.graph_event_id if link else None
        if not event_id:
            return None, "keine Outlook-Kennung zur Nachfrage vorhanden"
        if self.graph.get_event(user.mailbox, event_id, with_tanss_id=False) is not None:
            return None, "in Outlook weiterhin vorhanden — keine Löschung"
        return DeletionEvidence(
            proof="graph_404", probed_at=now, object_id=event_id,
            trigger="graph_deleted", http_status=404), ""

    def _delete(self, action: SyncAction, user: UserMapping) -> bool:
        """Führt eine Löschung aus — nach Freigabe durch den Wächter und mit Sicherung.

        Gibt ``False`` zurück, wenn stattdessen eine Karenzzeit läuft: Beim Wandeln
        einer Vormerkung in einen festen Termin löscht TANSS den alten Datensatz und
        legt Sekunden später einen neuen an. Ohne das Fenster wäre das ein Löschbefehl
        für einen Termin, der weiterlebt.
        """
        appointment = action.appointment
        link = self.state.get_link(appointment.key)
        if link is None:
            log.warning("Löschung ohne Verknüpfung übersprungen: %s", appointment.key)
            return False

        evidence = self._evidence.get(id(action))
        if evidence is None:
            log.warning("Löschung ohne Nachweis übersprungen: %s", appointment.key)
            return False

        allowed = self.guard.authorize(action, evidence, link)
        if not allowed.allowed:
            log.info("Löschung abgelehnt (%s): %s", appointment.key, allowed.reason)
            return False

        side = action.target_side.value
        pending = self.state.pending_deletion(appointment.key, side)

        if pending is None:
            self.guard.schedule(action, evidence, link)
            log.info("Löschung von %s vorgemerkt — Karenzzeit %s s läuft",
                     appointment.key, self.config.safety.deletion_grace_seconds)
            return False

        if int(pending["due_at"]) > int(datetime.now(UTC).timestamp()):
            log.debug("Karenzzeit für %s läuft noch", appointment.key)
            return False

        self.guard.snapshot_before_delete(appointment, side, evidence.trigger)

        if action.direction is SyncDirection.M365_TO_TANSS:
            self.tanss.delete_support(appointment.tanss_support_id)
        else:
            # is_organizer vergleicht gegen appointment.mailbox - ist es nicht
            # gesetzt, waere jede Loeschung faelschlich "nicht Organisator" und
            # die Warnung vor Absagemails bliebe aus.
            appointment.mailbox = appointment.mailbox or user.mailbox
            self.graph.delete_event(
                user.mailbox, appointment.graph_event_id,
                is_organizer=appointment.is_organizer,
                has_external_attendees=bool(
                    appointment.external_attendees(self.config.own_mail_domains)))

        self.state.mark_deletion_executed(int(pending["id"]))
        self.state.set_link_state(appointment.key, "deleted")
        return True

    # ------------------------------------------------------- Outlook -> TANSS

    def _create_in_tanss(self, action: SyncAction, user: UserMapping) -> None:
        """Einen in Outlook entstandenen Termin in TANSS anlegen.

        ``prevent_notification=True`` unterdrückt die TANSS-interne Benachrichtigung:
        Die Teilnehmer haben ihre Einladung bereits aus Outlook: eine zweite aus TANSS
        wäre für dieselbe Sache die zweite Mail.
        """
        appointment = action.appointment
        appointment.employee_id = appointment.employee_id or user.tanss_employee_id
        if not appointment.company_id:
            appointment.company_id = self.company.resolve(appointment)

        defaults = self.config.defaults_for(user.tanss_employee_id)
        if appointment.support_type_id is None:
            appointment.support_type_id = defaults.support_type_id

        appointment.origin = "OUTLOOK"
        appointment.is_internal = defaults.internal

        # Der Betreff traegt in Outlook das Firmensuffix - in TANSS gehoert es nicht hin.
        appointment.subject = self.subject.to_tanss(appointment.subject)

        write = self.tanss_mapper.to_write(
            appointment, for_update=False,
            own_company_id=self.config.tanss.own_company_id,
            graph_response=appointment.own_response)
        created = self.tanss.create_support(write, prevent_notification=True)

        appointment.tanss_support_id = created.id
        link = LinkRecord.for_new(appointment, user)
        link.tanss_support_id = created.id
        link.graph_event_id = appointment.graph_event_id
        after = self.tanss_mapper.to_appointment(created, user.mailbox,
                                                 appointment.key.uid)
        link.last_hash_tanss = fingerprint(after)
        link.last_hash_graph = fingerprint(appointment)
        link.last_seen_at = datetime.now(UTC)
        self.echo.mark_written(link, "tanss", link.last_hash_tanss)
        self.state.upsert_link(link)

        # Unsere Kennung an den Outlook-Termin, damit er auch ohne lokale Datenbank
        # wiederzufinden ist.
        try:
            self.graph.set_tanss_id(user.mailbox, appointment.graph_event_id, created.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Kennung am Outlook-Termin nicht gesetzt: %s. Der TANSS-Termin "
                        "ist angelegt und verknüpft.", exc)

    def _update_in_tanss(self, action: SyncAction, user: UserMapping) -> None:
        """Änderungen aus Outlook nach TANSS übertragen.

        Der vorhandene Stand wird zuerst gelesen — ``metaInfos`` werden in TANSS
        **ersetzt**, nicht ergänzt. Wer sie ohne den Bestand schreibt, löscht die
        Kopplung und alles andere gleich mit.
        """
        appointment = action.appointment
        if not appointment.tanss_support_id:
            log.warning("Update ohne Support-Kennung übersprungen: %s", appointment.key)
            return

        current = self.tanss.get_support(appointment.tanss_support_id)
        if current is None:
            # Kein Loeschnachweis an dieser Stelle - nur nichts zu aktualisieren.
            log.info("Support %s nicht mehr vorhanden — Änderung verworfen",
                     appointment.tanss_support_id)
            return

        appointment.subject = self.subject.to_tanss(appointment.subject)
        appointment.employee_id = appointment.employee_id or user.tanss_employee_id

        write = self.tanss_mapper.to_write(
            appointment, for_update=True, existing_meta=current.meta_infos,
            own_company_id=self.config.tanss.own_company_id,
            graph_response=appointment.own_response)

        try:
            updated = self.tanss.update_support(appointment.tanss_support_id, write)
        except ChangesDiscardedError:
            # Serverauskunft "nicht mehr dein Objekt" - etwa nach Wandlung in eine
            # Leistung. Das ist eine Entkopplung, keine Loeschung.
            log.info("TANSS hat die Änderung an %s verworfen — Kopplung wird beendet",
                     appointment.tanss_support_id)
            self.state.set_link_state(appointment.key, "detached")
            return

        link = (self.state.get_link(appointment.key)
                or LinkRecord.for_new(appointment, user))
        after = self.tanss_mapper.to_appointment(updated, user.mailbox,
                                                 appointment.key.uid)
        link.last_hash_tanss = fingerprint(after)
        link.last_hash_graph = fingerprint(appointment)
        link.last_seen_at = datetime.now(UTC)
        self.echo.mark_written(link, "tanss", link.last_hash_tanss)
        self.state.upsert_link(link)

    def _detach(self, action: SyncAction, user: UserMapping) -> None:
        """Kopplung beenden — **ohne** auf einer der beiden Seiten etwas zu löschen.

        Beide Termine bleiben bestehen. Sie gehen sich ab jetzt nur nichts mehr an.
        """
        log.info("Kopplung %s wird beendet: %s", action.appointment.key, action.reason)
        self.state.set_link_state(action.appointment.key, "detached")

    def _write_back_coupling(self, appointment) -> None:
        if not appointment.tanss_support_id:
            return
        current = self.tanss.get_support(appointment.tanss_support_id)
        if current is None:
            return  # inzwischen geloescht - nichts zu tun
        write = self.tanss_mapper.to_write(
            appointment, for_update=True, existing_meta=current.meta_infos,
            own_company_id=self.config.tanss.own_company_id)
        # Nur die Kopplung zurueckschreiben, keine Terminfelder.
        minimal = write.__class__(metaInfos=write.meta_infos)
        self.tanss.update_support(appointment.tanss_support_id, minimal)

    def _active_users(self) -> list[UserMapping]:
        from .directory import mappings_from_config

        return [u for u in mappings_from_config(self.config) if u.is_ready]

    def _window(self) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        return (now - timedelta(days=self.config.sync.window_days_past),
                now + timedelta(days=self.config.sync.window_days_future))


def _bump(report: RunReport, operation: SyncOperation) -> None:
    if operation is SyncOperation.CREATE:
        report.created += 1
    elif operation is SyncOperation.UPDATE:
        report.updated += 1
    elif operation is SyncOperation.DELETE:
        report.deleted += 1
