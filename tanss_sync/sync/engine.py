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
from ..domain.identity import UserMapping
from ..domain.uid import canonical_uid
from ..microsoft.mapper import GraphMapper
from ..microsoft.repository import GraphRepository
from ..state.records import LinkRecord, RunReport
from ..state.store import StateStore
from ..tanss.mapper import TanssMapper
from ..tanss.repository import TanssRepository
from ..util.html import HtmlText
from ..util.timezone import TimeConverter
from .compare import fingerprint
from .deletion import DeletionGuard
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
        self.rules = SyncRules(config.sync)
        self.echo = EchoGuard(config.sync.echo_suppression_seconds)
        self.reconciler = Reconciler(self.rules, self.echo)
        self.guard = DeletionGuard(config.safety, state,
                                   own_domains=config.own_mail_domains)
        self.holder = f"{os.getpid()}:{threading.current_thread().name}"

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
            except Exception as exc:  # noqa: BLE001 - ein Benutzer darf den Lauf nicht kippen
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

        for appointment, reason in changes.skipped:
            report.skipped += 1
            audit.record_skip(appointment, reason)

        # Ausgangsmarken setzen - ohne zu schreiben. Das ist der Grund, warum der
        # erste Lauf gegen einen Bestand ruhig bleibt: Er merkt sich, wie beide
        # Seiten aussehen, statt sie aneinander anzugleichen.
        if not dry_run:
            for pair, state in changes.baselines:
                self._store_baseline(pair, state, user)

        creates = [a for a in changes.actions if a.operation is SyncOperation.CREATE]
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
                uid = f"pending:{support.id}"
            appointment = self.tanss_mapper.to_appointment(support, user.mailbox, uid)
            if sequence >= 0:
                appointment.key = appointment.key.__class__(
                    user.mailbox, uid, sequence, "main")
            out.append(appointment)
        return out

    def _apply(self, action: SyncAction, user: UserMapping, audit: AuditLogger,
               report: RunReport, *, dry_run: bool) -> None:
        appointment = action.appointment

        if dry_run:
            audit.record(action, outcome=Outcome.DRY_RUN)
            _bump(report, action.operation)
            return

        started = datetime.now(UTC)
        try:
            if action.operation is SyncOperation.CREATE:
                self._create_in_graph(action, user)
            elif action.operation is SyncOperation.UPDATE:
                self._update_in_graph(action, user)
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
