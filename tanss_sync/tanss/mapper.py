"""Umrechnung zwischen ``TanssSupport`` und ``Appointment``.

Die gesamte TANSS-Feldkenntnis liegt hier. Wer etwas über das Datenmodell wissen will,
liest diese Datei — nicht den Reconciler.
"""

from __future__ import annotations

from datetime import datetime

from ..domain.appointment import (
    Appointment,
    AppointmentKind,
    Attendee,
    ServiceLocation,
    TravelTime,
)
from ..domain.identity import SyncKey
from ..domain.uid import NO_SEQUENCE, format_sync_group, parse_sync_group
from ..util.html import HtmlText
from ..util.timezone import TimeConverter
from .models import LINK_TYPE_COMPANY, MetaKey, TanssPlanningType, TanssSupport, TanssSupportWrite

# TANSS-Typ -> fachliche Art
_KIND_BY_TYPE = {
    TanssPlanningType.NONE: AppointmentKind.UNKNOWN,
    TanssPlanningType.SUPPORT: AppointmentKind.WORK_LOG,
    TanssPlanningType.APPOINTMENT_PROPOSAL: AppointmentKind.TENTATIVE,
    TanssPlanningType.APPOINTMENT_FIX: AppointmentKind.FIXED,
    TanssPlanningType.APPOINTMENT_PRIVATE: AppointmentKind.PRIVATE,
    TanssPlanningType.VACATION: AppointmentKind.VACATION,
    TanssPlanningType.ILLNESS: AppointmentKind.ILLNESS,
    TanssPlanningType.ABSENCE: AppointmentKind.ABSENCE,
    TanssPlanningType.OVERTIME: AppointmentKind.OVERTIME,
    TanssPlanningType.STAND_BY: AppointmentKind.STANDBY,
    TanssPlanningType.CUSTOM: AppointmentKind.CUSTOM,
}

_TYPE_BY_KIND = {
    AppointmentKind.FIXED: TanssPlanningType.APPOINTMENT_FIX,
    AppointmentKind.TENTATIVE: TanssPlanningType.APPOINTMENT_PROPOSAL,
    AppointmentKind.PRIVATE: TanssPlanningType.APPOINTMENT_PRIVATE,
}

# Graph-Antwortstatus -> TANSS-Zustand. Keine 1:1-Zuordnung: Graph kennt sechs Werte,
# TANSS drei. "Unter Vorbehalt" gibt es in TANSS nicht - es zaehlt als volle Zusage.
STATUS_FROM_GRAPH = {
    "none": "REQUESTED",
    "notResponded": "REQUESTED",
    "accepted": "ACCEPTED",
    "organizer": "ACCEPTED",
    "tentativelyAccepted": "ACCEPTED",
    "declined": "CANCELED",
}


class TanssMapper:
    def __init__(self, time: TimeConverter, *, company_names: dict[int, str] | None = None,
                 html: HtmlText | None = None) -> None:
        self.time = time
        self.company_names = company_names or {}
        self.html = html or HtmlText()

    # ------------------------------------------------------------------ lesen

    def to_appointment(self, support: TanssSupport, mailbox: str, uid: str) -> Appointment:
        """``uid`` wird übergeben, nicht abgeleitet.

        Bei einer Serien-Occurrence stammt sie vom Master — der Mapper kann das nicht
        selbst wissen, weil er den Graph nicht kennt.
        """
        start = self.time.tanss_to_utc(support.date)
        sequence = (support.recurrence_rule_sequence_id
                    if support.recurrence_rule_sequence_id is not None else NO_SEQUENCE)

        key = SyncKey(mailbox=mailbox, uid=uid, sequence=sequence)
        kind = _KIND_BY_TYPE.get(support.planning_type, AppointmentKind.UNKNOWN)

        return Appointment(
            key=key,
            tanss_support_id=support.id or None,
            recurrence_rule_id=support.recurrence_rule_id,
            subject=support.outlook_title.strip(),
            body=support.text,
            location=support.outlook_location,
            start=start,
            end=self.time.end_of(start, support.duration),
            kind=kind,
            service_location=support.location,
            show_as="oof" if kind.is_absence else "busy",
            is_internal=support.internal,
            employee_id=support.employee_id,
            mailbox=mailbox,
            company_id=support.company_id or None,
            company_name=self.company_names.get(support.company_id),
            ticket_id=support.ticket_id or None,
            support_type_id=support.type_id or None,
            vacation_request_id=support.vacation_request_id or None,
            attendees=[Attendee(email=a) for a in support.outlook_participants],
            teams_url=support.teams_url,
            travel=TravelTime(
                minutes_before=support.duration_approach,
                minutes_after=support.duration_departure,
                km_before=support.km_approach,
                km_after=support.km_departure,
            ),
            # startBreak ist ein ABSOLUTER Unix-Zeitstempel, kein Minutenversatz.
            break_start=(self.time.tanss_to_utc(support.start_break)
                         if support.start_break else None),
            break_minutes=support.duration_break,
            origin="OUTLOOK" if support.came_from_outlook else "TANSS",
            tanss_hash=self.html.extract_tanss_hash(support.text),
            # None = nicht abgefragt. Die Listenantwort liefert das Feld nie.
            outlook_read_only=support.outlook_read_only,
            created=(self.time.tanss_to_utc(support.date_created)
                     if support.date_created else None),
            last_modified=(self.time.tanss_to_utc(support.modified)
                           if support.modified else None),
        )

    # ------------------------------------------------------------------ schreiben

    def to_write(self, appointment: Appointment, *, for_update: bool,
                 existing_meta: dict[str, str] | None = None,
                 own_company_id: int = 0,
                 graph_response: str | None = None) -> TanssSupportWrite:
        """Baut den Schreib-Payload.

        Zwei Regeln, die hier und nirgends sonst durchgesetzt werden:

        * **``metaInfos`` vollständig oder gar nicht.** Ein Teilsatz löscht den Rest —
          maßgeblich ist, dass das Feld vorhanden ist, nicht dass es unvollständig wäre.
        * **Firma immer zu dritt.** ``companyId`` allein erzeugt einen Termin ohne
          Zuordnung; die hängt am Paar ``linkTypeId``/``linkId``.
        """
        write = TanssSupportWrite()

        if appointment.start:
            write.date = self.time.utc_to_tanss(appointment.start)
            write.duration = appointment.duration_minutes
        if appointment.employee_id:
            write.employee_id = appointment.employee_id

        company = appointment.company_id or own_company_id or None
        if company:
            write.set_company(company)

        if appointment.ticket_id:
            write.ticket_id = appointment.ticket_id
        if appointment.support_type_id is not None:
            write.type_id = appointment.support_type_id

        planning = _TYPE_BY_KIND.get(appointment.kind)
        if planning is not None:
            write.planning_type = planning
        write.location = appointment.service_location or ServiceLocation.OFFICE
        write.internal = appointment.is_internal
        write.text = appointment.body
        write.outlook_title = appointment.subject
        write.outlook_location = appointment.location

        if appointment.travel.has_any():
            write.duration_approach = appointment.travel.minutes_before
            write.duration_departure = appointment.travel.minutes_after

        if appointment.key.sequence >= 0 and appointment.recurrence_rule_id:
            write.recurrence_master_link_id = appointment.recurrence_rule_id
            write.recurrence_rule_sequence_id = appointment.key.sequence

        meta = self.build_meta(appointment, existing_meta=existing_meta,
                              graph_response=graph_response)
        if meta is not None:
            write.meta_infos = meta

        return write

    def build_meta(self, appointment: Appointment, *,
                   existing_meta: dict[str, str] | None,
                   graph_response: str | None = None) -> dict[str, str] | None:
        """Der vollständige Meta-Satz — oder ``None``, wenn nichts zu ändern ist.

        ``None`` lässt das Feld aus dem Payload heraus und die vorhandenen Einträge
        unberührt. Ein leeres ``{}`` würde sie dagegen **alle löschen**, inklusive der
        Kopplung.
        """
        if not appointment.key.uid:
            return None

        meta: dict[str, str] = dict(existing_meta or {})
        meta[MetaKey.SYNC_GROUP] = format_sync_group(appointment.key.uid,
                                                     appointment.key.sequence)
        if appointment.origin:
            meta[MetaKey.SYNC_ORIGIN] = appointment.origin
        if appointment.attendees:
            meta[MetaKey.OUTLOOK_PARTICIPANTS] = ",".join(
                a.email for a in appointment.attendees if a.email)
        if appointment.teams_url:
            meta[MetaKey.TEAMS_URL] = appointment.teams_url
        if graph_response:
            mapped = STATUS_FROM_GRAPH.get(graph_response)
            if mapped:
                meta[MetaKey.SYNC_APPOINTMENT_STATUS] = mapped

        # TEXT_WAS_CHANGED setzt der Server selbst - unveraendert uebernehmen.
        return meta

    # ------------------------------------------------------------------ Hilfen

    @staticmethod
    def uid_of(support: TanssSupport) -> tuple[str, int]:
        """Kopplungs-UID und Sequenz eines Supports.

        Beim Lesen trägt ``SYNC_GROUP`` in aller Regel keine Sequenz — Occurrences erben
        die Metadaten des Masters. Maßgeblich ist deshalb
        ``recurrenceRuleSequenceId`` am Support selbst.
        """
        uid, seq_from_group = parse_sync_group(support.sync_group)
        sequence = (support.recurrence_rule_sequence_id
                    if support.recurrence_rule_sequence_id is not None
                    else seq_from_group)
        return uid, sequence

    @staticmethod
    def is_company_linked(support: TanssSupport) -> bool:
        return support.link_type_id == LINK_TYPE_COMPANY and support.link_id > 0


def epoch(value: datetime) -> int:
    return int(value.timestamp())
