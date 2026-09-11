"""Umrechnung zwischen ``GraphEvent`` und ``Appointment``."""

from __future__ import annotations

from ..domain.appointment import Appointment, AppointmentKind, Attendee
from ..domain.identity import SyncKey
from ..domain.uid import NO_SEQUENCE
from ..util.html import HtmlText
from ..util.timezone import TimeConverter
from .models import GraphEvent

# Felder des Appointment -> Graph-Eigenschaften. Nur was hier steht, wird geschrieben.
_UPDATABLE = {
    "subject": "subject",
    "body": "body",
    "location": "location",
    "start": "start",
    "end": "end",
    "show_as": "showAs",
}


class GraphMapper:
    def __init__(self, time: TimeConverter, *, html: HtmlText | None = None) -> None:
        self.time = time
        self.html = html or HtmlText()

    # ------------------------------------------------------------------ lesen

    def to_appointment(self, event: GraphEvent, mailbox: str, uid: str,
                       *, own_domains: set[str] | None = None) -> Appointment:
        own_domains = own_domains or set()
        body_text = self.html.to_text(event.body.get("content", ""))

        kind = AppointmentKind.FIXED
        if event.sensitivity == "private":
            kind = AppointmentKind.PRIVATE
        elif event.response in ("none", "notResponded"):
            kind = AppointmentKind.TENTATIVE

        attendees = [
            Attendee(
                email=(a.get("emailAddress", {}) or {}).get("address", "").lower(),
                name=(a.get("emailAddress", {}) or {}).get("name"),
                required=a.get("type") != "optional",
                response=(a.get("status", {}) or {}).get("response", "none"),
                is_organizer=False,
            )
            for a in event.attendees
            if (a.get("emailAddress", {}) or {}).get("address")
        ]
        organizer = event.organizer_address
        if organizer:
            attendees.append(Attendee(email=organizer, is_organizer=True,
                                      response="organizer"))

        return Appointment(
            key=SyncKey(mailbox=mailbox, uid=uid, sequence=NO_SEQUENCE),
            graph_event_id=event.id,
            series_master_id=event.series_master_id,
            subject=event.subject,
            body=body_text,
            location=(event.location or {}).get("displayName", ""),
            start=self.time.from_graph(event.start),
            end=self.time.from_graph(event.end),
            all_day=event.is_all_day,
            kind=kind,
            show_as=event.show_as,  # type: ignore[arg-type]
            is_cancelled=event.is_cancelled,
            mailbox=mailbox,
            attendees=attendees,
            teams_url=event.teams_join_url or self.html.extract_teams_url(body_text),
            tanss_hash=self.html.extract_tanss_hash(body_text),
            origin="OUTLOOK",
            created=event.created,
            last_modified=event.last_modified,
            tanss_support_id=event.tanss_support_id,
        )

    # ------------------------------------------------------------------ schreiben

    def to_create_payload(self, appointment: Appointment, *,
                          include_attendees: bool = False) -> dict:
        """Payload für einen neuen Termin.

        **Ohne ``attendees``**, wo immer möglich: Graph verschickt beim Anlegen eines
        Termins mit Teilnehmern automatisch Einladungen an alle — das lässt sich nicht
        abschalten. Ein Termin, der ohnehin nur den Kalender des Mitarbeiters füllen
        soll, braucht keine Teilnehmer und löst dann auch keine Mail aus.
        """
        payload: dict = {
            "subject": appointment.subject,
            "body": {"contentType": "text", "content": appointment.body},
            "start": self.time.to_graph(appointment.start),
            "end": self.time.to_graph(appointment.end),
            "showAs": appointment.show_as,
            "isAllDay": appointment.all_day,
        }
        if appointment.location:
            payload["location"] = {"displayName": appointment.location}
        if appointment.kind is AppointmentKind.PRIVATE:
            payload["sensitivity"] = "private"
        if include_attendees and appointment.attendees:
            payload["attendees"] = [
                {"emailAddress": {"address": a.email, "name": a.name or a.email},
                 "type": "required" if a.required else "optional"}
                for a in appointment.attendees if not a.is_organizer
            ]
        return payload

    def to_update_payload(self, appointment: Appointment, changed: set[str], *,
                          existing_body: str | None = None) -> dict:
        """Nur die tatsächlich geänderten Felder.

        ``attendees`` fehlt hier bewusst: Schon das bloße Mitsenden löst
        Aktualisierungsmails an alle Teilnehmer aus. Teilnehmer pflegt dieses Werkzeug
        nicht — sie kommen aus Outlook und bleiben dort.
        """
        payload: dict = {}
        for field in changed:
            target = _UPDATABLE.get(field)
            if target is None:
                continue
            if field == "body":
                # Teams-Block und TANSS-Hash aus dem bestehenden Text retten.
                merged = self.html.preserve_blocks(existing_body, appointment.body)
                payload["body"] = {"contentType": "text", "content": merged}
            elif field == "location":
                payload["location"] = {"displayName": appointment.location}
            elif field in ("start", "end"):
                payload[target] = self.time.to_graph(getattr(appointment, field))
            else:
                payload[target] = getattr(appointment, field)
        return payload
