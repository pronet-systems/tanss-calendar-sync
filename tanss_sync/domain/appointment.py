"""Das systemneutrale Zwischenmodell.

Beide Systeme werden hierauf abgebildet. Der Sync-Kern kennt weder TANSS noch Graph —
das hält die Vergleichslogik an einer Stelle und macht sie testbar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from .identity import SyncKey, TravelRole
from .recurrence import RecurrencePattern

ShowAs = Literal["free", "tentative", "busy", "oof", "workingElsewhere", "unknown"]
ReadOnlyFlag = Literal["NO", "TEXT_READ_ONLY", "TEXT_TITLE_LOCATION_READ_ONLY"]
AttendeeResponse = Literal[
    "none", "organizer", "accepted", "tentative", "declined", "notResponded"
]


class ServiceLocation(StrEnum):
    """TANSS-Leistungsort. Zahlenwerte: OFFICE=1, CUSTOMER=2, REMOTE=3."""

    OFFICE = "OFFICE"
    CUSTOMER = "CUSTOMER"
    REMOTE = "REMOTE"


class AppointmentKind(StrEnum):
    """Fachliche Art eines Termins — unabhängig von beiden Systemen."""

    UNKNOWN = "unknown"
    FIXED = "fixed"
    TENTATIVE = "tentative"
    PRIVATE = "private"
    VACATION = "vacation"
    ILLNESS = "illness"
    ABSENCE = "absence"
    OVERTIME = "overtime"
    STANDBY = "standby"
    CUSTOM = "custom"
    WORK_LOG = "work_log"
    TRAVEL_TO = "travel_to"
    TRAVEL_BACK = "travel_back"

    @property
    def is_appointment(self) -> bool:
        return self in (
            AppointmentKind.FIXED,
            AppointmentKind.TENTATIVE,
            AppointmentKind.PRIVATE,
        )

    @property
    def is_absence(self) -> bool:
        """Urlaub, Krankheit, Abwesenheit, Überstunden, Bereitschaft, Custom.

        Folgt **bewusst nicht** der TANSS-internen Einordnung, die ``CUSTOM``
        ausschließt: Die FAQ verlangt Custom-Abwesenheiten ausdrücklich, und ohne
        diese Zuordnung bekäme ein Custom-Eintrag ``write_direction = both`` — ein
        Löschen in Outlook würde dann den echten TANSS-Datensatz entfernen.
        """
        return self in (
            AppointmentKind.VACATION,
            AppointmentKind.ILLNESS,
            AppointmentKind.ABSENCE,
            AppointmentKind.OVERTIME,
            AppointmentKind.STANDBY,
            AppointmentKind.CUSTOM,
        )

    @property
    def is_travel(self) -> bool:
        return self in (AppointmentKind.TRAVEL_TO, AppointmentKind.TRAVEL_BACK)

    @property
    def syncs_to_outlook(self) -> bool:
        """Getätigte Leistungen und Unbekanntes gehen nie nach Outlook."""
        return self not in (AppointmentKind.WORK_LOG, AppointmentKind.UNKNOWN)

    @property
    def syncs_to_tanss(self) -> bool:
        """Abwesenheiten und Fahrten sind reine Projektionen — nie zurück nach TANSS."""
        return not (
            self.is_absence
            or self.is_travel
            or self in (AppointmentKind.WORK_LOG, AppointmentKind.UNKNOWN)
        )


@dataclass(frozen=True, slots=True)
class Attendee:
    email: str
    name: str | None = None
    required: bool = True
    response: AttendeeResponse = "none"
    is_organizer: bool = False

    @property
    def domain(self) -> str:
        _, _, domain = self.email.rpartition("@")
        return domain.lower()


@dataclass(frozen=True, slots=True)
class TravelTime:
    """An- und Abfahrt. In TANSS Felder am Haupttermin, in Outlook eigene Einträge."""

    minutes_before: int = 0
    minutes_after: int = 0
    km_before: int = 0
    km_after: int = 0

    def has_any(self) -> bool:
        return self.minutes_before > 0 or self.minutes_after > 0


# Felder, die den Inhalts-Hash bilden. Bewusst eng gehalten: Alles, was hier steht,
# loest bei Abweichung einen Schreibvorgang aus.
_HASH_FIELDS = (
    "subject",
    "body",
    "location",
    "start",
    "end",
    "all_day",
    "kind",
    "service_location",
    "show_as",
    "is_internal",
    "company_id",
    "ticket_id",
    "support_type_id",
)


@dataclass(slots=True)
class Appointment:
    """Systemneutrale Repräsentation eines Termins."""

    key: SyncKey

    # Identitaeten in beiden Systemen
    tanss_support_id: int | None = None
    graph_event_id: str | None = None
    series_master_id: str | None = None
    recurrence_rule_id: int | None = None

    # Inhalt
    subject: str = ""
    body: str = ""  # immer Klartext
    location: str = ""
    start: datetime | None = None  # timezone-aware UTC
    end: datetime | None = None
    all_day: bool = False

    # Klassifizierung
    kind: AppointmentKind = AppointmentKind.FIXED
    service_location: ServiceLocation = ServiceLocation.OFFICE
    show_as: ShowAs = "busy"
    is_internal: bool = True
    is_cancelled: bool = False
    outlook_read_only: ReadOnlyFlag | None = None  # None = UNBEKANNT, nicht ungeschuetzt

    # Kontext
    employee_id: int | None = None
    mailbox: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    ticket_id: int | None = None
    support_type_id: int | None = None
    vacation_request_id: int | None = None  # Gruppierung fuer den Not-Aus

    # Extras
    attendees: list[Attendee] = field(default_factory=list)
    # Outlook-Kategorien. Interessant ist genau eine Sorte: die Markierung, die eine
    # andere Terminsynchronisation an von ihr betreute Termine haengt.
    categories: list[str] = field(default_factory=list)
    teams_url: str | None = None
    travel: TravelTime = field(default_factory=TravelTime)
    break_start: datetime | None = None
    break_minutes: int = 0
    recurrence: RecurrencePattern | None = None
    # Die TANSS-eigene Nummer der Occurrence. Getrennt vom Schluessel gefuehrt: Der
    # Schluessel muss auf beiden Seiten gleich entstehen, diese Nummer gibt es nur in
    # TANSS - sie wird dort aber beim Schreiben erwartet.
    recurrence_sequence_id: int | None = None
    is_occurrence: bool = False
    origin: Literal["TANSS", "OUTLOOK"] | None = None
    # Antwort des Postfachinhabers auf die Einladung. Nicht zu verwechseln mit der
    # Antwort einzelner Teilnehmer - massgeblich fuer SYNC_APPOINTMENT_STATUS ist
    # allein, wie DIESER Mitarbeiter zugesagt hat.
    own_response: str | None = None
    tanss_hash: str | None = None  # Add-in-Marker im Text, unveraendert erhalten

    # Aenderungsverfolgung
    created: datetime | None = None  # Anlagezeitpunkt - Kriterium fuer activated_at
    last_modified: datetime | None = None

    # ------------------------------------------------------------------ Ableitungen

    @property
    def duration_minutes(self) -> int:
        if not self.start or not self.end:
            return 0
        return max(0, int((self.end - self.start).total_seconds() // 60))

    @property
    def is_organizer(self) -> bool:
        return any(a.is_organizer and a.email.lower() == (self.mailbox or "").lower()
                   for a in self.attendees)

    def external_attendees(self, own_domains: set[str]) -> list[Attendee]:
        """Teilnehmer ausserhalb der eigenen Maildomänen.

        Entscheidet mit darüber, ob eine Löschung eine Absagemail an Kunden auslöst.
        """
        return [a for a in self.attendees if a.domain and a.domain not in own_domains]

    def content_hash(self) -> str:
        """Prüfsumme über die sync-relevanten Felder.

        **Immer aus der Serverantwort bilden, nie aus dem gesendeten Payload.**
        TANSS normalisiert beim Speichern — Rundung verändert die Dauer, eine Zusage
        wandelt eine Vormerkung in einen festen Termin. Ein Hash über den Payload passt
        nie auf das, was beim nächsten Lauf zurückkommt: Jede eigene Schreibung käme als
        fremde Änderung zurück, und der Abgleich liefe im Kreis.
        """
        payload: dict[str, object] = {}
        for name in _HASH_FIELDS:
            value = getattr(self, name)
            if isinstance(value, datetime):
                payload[name] = value.astimezone(tz=value.tzinfo).isoformat()
            elif isinstance(value, StrEnum):
                payload[name] = str(value)
            else:
                payload[name] = value
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def differs_from(self, other: Appointment) -> set[str]:
        """Namen der abweichenden Felder — für Teil-Updates und das Protokoll."""
        return {name for name in _HASH_FIELDS
                if getattr(self, name) != getattr(other, name)}

    # ------------------------------------------------------------------ Fahrtzeiten

    def split_travel(self) -> list[Appointment]:
        """Zerlegt in ``[Anfahrt], Termin, [Abfahrt]`` für die Outlook-Seite.

        Nur bei ``service_location == CUSTOMER``: TANSS blockt Fahrtzeit ausschließlich
        bei Vor-Ort-Terminen, auch wenn die Felder bei anderen Orten gefüllt sind.
        Es können auch nur eine oder gar keine Fahrt entstehen — „zwei" ist nicht
        garantiert.
        """
        if not self.kind.syncs_to_outlook:
            # Eine Fahrt ist die Projektion ihres Haupttermins. Geht der nicht nach
            # Outlook - eine getaetigte Leistung tut das nie -, dann sie auch nicht.
            # Sonst stehen Anfahrt und Abfahrt im Kalender und zwischen ihnen fehlt
            # der Termin, zu dem sie gehoeren.
            return [self]
        if self.service_location is not ServiceLocation.CUSTOMER or not self.travel.has_any():
            return [self]
        if not self.start or not self.end:
            return [self]

        out: list[Appointment] = []
        if self.travel.minutes_before > 0:
            out.append(self._travel_leg("travel_to", AppointmentKind.TRAVEL_TO,
                                        self.start - timedelta(minutes=self.travel.minutes_before),
                                        self.start))
        out.append(self)
        if self.travel.minutes_after > 0:
            out.append(self._travel_leg("travel_back", AppointmentKind.TRAVEL_BACK,
                                        self.end,
                                        self.end + timedelta(minutes=self.travel.minutes_after)))
        return out

    def _travel_leg(self, role: TravelRole, kind: AppointmentKind,
                    start: datetime, end: datetime) -> Appointment:
        label = "Anfahrt" if kind is AppointmentKind.TRAVEL_TO else "Abfahrt"
        return Appointment(
            key=self.key.with_travel(role),
            tanss_support_id=self.tanss_support_id,  # alle drei teilen sie sich!
            employee_id=self.employee_id,
            mailbox=self.mailbox,
            # Bewusst nur das Wort, ohne Termintitel und ohne Firmensuffix. Wohin die
            # Fahrt geht, steht im Ortsfeld - und genau so heissen die Bloecke in
            # gewachsenen Kalendern bereits. Ein abweichender Titel verhinderte, dass
            # bestehende Bloecke wiedererkannt werden, und verdoppelte sie.
            subject=label,
            # Ort und Adresse des Kunden gehoeren dazu - ohne Adresse ist ein
            # Fahrtblock im Kalender wertlos.
            location=self.location,
            start=start,
            end=end,
            kind=kind,
            service_location=self.service_location,
            show_as="busy",
            is_internal=self.is_internal,
            company_id=self.company_id,
            company_name=self.company_name,
            origin=self.origin,
        )
