"""TANSS-Datenmodelle.

Lesen und Schreiben sind **bewusst getrennt**. Zwei Felder sind im Schreibpfad keine
Nutzdaten, sondern Steuerbefehle mit Löschwirkung:

``metaInfos``
    Maßgeblich ist *vorhanden*, nicht *unvollständig*. Schon ein leeres ``{}`` löscht
    alle Meta-Einträge des Supports — inklusive ``SYNC_GROUP``. Die Kopplung reißt, und
    beim nächsten Lauf entsteht ein Duplikat.

``linkedTechnicians``
    Mitgesendet legt der Server Supports für hinzugekommene Techniker an und **löscht
    die der entfernten**. Ein leeres ``[]`` löscht die Termine aller anderen internen
    Teilnehmer einer Besprechung. Im Schreibmodell existiert das Feld deshalb nicht.

Beide Löschungen liefen außerhalb des Löschpfads — vorbei am Wächter, am Not-Aus und
an jeder Invariante.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..domain.appointment import ServiceLocation

LINK_TYPE_COMPANY = 2  # linkTypeId fuer eine Firmenzuordnung


class TanssPlanningType(StrEnum):
    """Die elf Werte des Servers, serialisiert als Name."""

    NONE = "NONE"
    SUPPORT = "SUPPORT"
    APPOINTMENT_PROPOSAL = "APPOINTMENT_PROPOSAL"
    APPOINTMENT_FIX = "APPOINTMENT_FIX"
    VACATION = "VACATION"
    ILLNESS = "ILLNESS"
    ABSENCE = "ABSENCE"
    STAND_BY = "STAND_BY"
    APPOINTMENT_PRIVATE = "APPOINTMENT_PRIVATE"
    OVERTIME = "OVERTIME"
    CUSTOM = "CUSTOM"


class MetaKey(StrEnum):
    """Die sechs Schlüssel in ``leistungen_meta``."""

    OUTLOOK_PARTICIPANTS = "OUTLOOK_PARTICIPANTS"
    SYNC_ORIGIN = "SYNC_ORIGIN"
    TEAMS_URL = "TEAMS_URL"
    SYNC_GROUP = "SYNC_GROUP"
    SYNC_APPOINTMENT_STATUS = "SYNC_APPOINTMENT_STATUS"
    TEXT_WAS_CHANGED = "TEXT_WAS_CHANGED"


class TanssSupport(BaseModel):
    """Lesemodell. Wird nie serialisiert.

    ``None`` heißt „nicht abgefragt" — mit einer Ausnahme: ``metaInfos`` lässt der
    Server auch dann weg, wenn der Support schlicht keine Metadaten hat. Ob überhaupt
    abgefragt wurde, sagt :class:`SupportPage`, nicht das Feld.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    id: int  # 0 bei virtuellen Serien-Occurrences!
    date: int  # Unix-SEKUNDEN (UTC)
    duration: int  # MINUTEN
    employee_id: int = Field(alias="employeeId")
    company_id: int = Field(alias="companyId")
    link_type_id: int = Field(alias="linkTypeId", default=0)
    link_id: int = Field(alias="linkId", default=0)
    ticket_id: int = Field(alias="ticketId", default=0)
    type_id: int = Field(alias="typeId", default=0)
    planning_type: TanssPlanningType = Field(alias="planningType")
    location: ServiceLocation = ServiceLocation.OFFICE
    text: str = ""
    text_intern: str = Field(alias="textIntern", default="")
    internal: bool = True
    extern: bool = False
    outlook: bool = False
    outlook_title: str = Field(alias="outlookTitle", default="")
    outlook_location: str = Field(alias="outlookLocation", default="")
    duration_approach: int = Field(alias="durationApproach", default=0)
    duration_departure: int = Field(alias="durationDeparture", default=0)
    km_approach: int = Field(alias="kmApproach", default=0)
    km_departure: int = Field(alias="kmDeparture", default=0)
    # ABSOLUTER Unix-Zeitstempel in Sekunden, kein Minutenversatz!
    start_break: int = Field(alias="startBreak", default=0)
    duration_break: int = Field(alias="durationBreak", default=0)  # Minuten
    vacation_request_id: int = Field(alias="vacationRequestId", default=0)

    # Serienkennung - NUR hierueber wird eine Occurrence identifiziert
    recurrence_rule_id: int | None = Field(alias="recurrenceRuleId", default=None)
    recurrence_master_link_id: int | None = Field(alias="recurrenceMasterLinkId", default=None)
    recurrence_rule_sequence_id: int | None = Field(alias="recurrenceRuleSequenceId",
                                                    default=None)

    # Nur im Einzelabruf enthalten: None = NICHT ABGEFRAGT
    outlook_read_only: str | None = Field(alias="outlookReadOnly", default=None)
    appointment_series_id: int | None = Field(alias="appointmentSeriesId", default=None)
    group_id: int | None = Field(alias="groupId", default=None)
    modified: int | None = None
    date_created: int | None = Field(alias="dateCreated", default=None)
    linked_technicians: list[dict] | None = Field(alias="linkedTechnicians", default=None)

    # Fehlt auch bei abgefragten Supports ohne Metadaten -> {} heisst hier "keine".
    meta_infos: dict[str, str] = Field(alias="metaInfos", default_factory=dict)

    @property
    def is_occurrence(self) -> bool:
        """Virtuelle, aus der Regel berechnete Occurrence ohne eigenen Datensatz."""
        return self.id == 0 and self.recurrence_rule_sequence_id is not None

    @property
    def sync_group(self) -> str | None:
        return self.meta_infos.get(MetaKey.SYNC_GROUP)

    @property
    def sync_origin(self) -> str | None:
        return self.meta_infos.get(MetaKey.SYNC_ORIGIN)

    @property
    def sync_status(self) -> str | None:
        return self.meta_infos.get(MetaKey.SYNC_APPOINTMENT_STATUS)

    @property
    def teams_url(self) -> str | None:
        return self.meta_infos.get(MetaKey.TEAMS_URL)

    @property
    def outlook_participants(self) -> list[str]:
        raw = self.meta_infos.get(MetaKey.OUTLOOK_PARTICIPANTS, "")
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def came_from_outlook(self) -> bool:
        return self.sync_origin == "OUTLOOK"


@dataclass(slots=True)
class SupportPage:
    """Ergebnis einer Listenabfrage.

    Trägt mit, **was** abgefragt wurde. Ohne diese Angabe würde aus einem fehlenden
    Feld eine inhaltliche Aussage abgeleitet — bei ``metaInfos`` hieße das
    „keine SYNC_GROUP" und damit „nicht gekoppelt": ein Duplikat-Lauf.
    """

    items: list[TanssSupport]
    employee_id: int  # genau EIN Mitarbeiter je Aufruf - activated_at ist je Benutzer
    meta_fetched: bool
    recurring_expanded: bool
    created_from: datetime | None  # gesetzter creationTimeframe, None = keiner
    linked_entities: dict

    def __len__(self) -> int:
        return len(self.items)


class TanssSupportWrite(BaseModel):
    """Schreibmodell. Serialisiert ausschließlich gesetzte Felder.

    ``linkedTechnicians`` fehlt hier absichtlich — siehe Modulkopf.
    Vom Server beim Update ohnehin verworfen und deshalb nicht enthalten:
    ``id``, ``persistOptions``, ``dateExport``, ``dateCreated``, ``modified``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    date: int | None = None
    duration: int | None = None
    employee_id: int | None = Field(alias="employeeId", default=None)
    company_id: int | None = Field(alias="companyId", default=None)
    link_type_id: int | None = Field(alias="linkTypeId", default=None)
    link_id: int | None = Field(alias="linkId", default=None)
    ticket_id: int | None = Field(alias="ticketId", default=None)
    type_id: int | None = Field(alias="typeId", default=None)
    planning_type: TanssPlanningType | None = Field(alias="planningType", default=None)
    location: ServiceLocation | None = None
    text: str | None = None
    outlook_title: str | None = Field(alias="outlookTitle", default=None)
    outlook_location: str | None = Field(alias="outlookLocation", default=None)
    internal: bool | None = None
    duration_approach: int | None = Field(alias="durationApproach", default=None)
    duration_departure: int | None = Field(alias="durationDeparture", default=None)
    recurrence_master_link_id: int | None = Field(alias="recurrenceMasterLinkId", default=None)
    recurrence_rule_sequence_id: int | None = Field(alias="recurrenceRuleSequenceId",
                                                    default=None)
    meta_infos: dict[str, str] | None = Field(alias="metaInfos", default=None)

    def payload(self) -> dict:
        return self.model_dump(by_alias=True, exclude_unset=True, exclude_none=True)

    def set_company(self, company_id: int) -> None:
        """Firma setzen — immer alle drei Felder zusammen.

        ``companyId`` allein erzeugt einen Termin **ohne** Zuordnung; die hängt am Paar
        ``linkTypeId``/``linkId``.
        """
        self.company_id = company_id
        self.link_type_id = LINK_TYPE_COMPANY
        self.link_id = company_id


class TanssEmployee(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    name: str = ""
    first_name: str = Field(alias="firstName", default="")
    last_name: str = Field(alias="lastName", default="")
    email_address: str = Field(alias="emailAddress", default="")

    @property
    def mail_domain(self) -> str:
        _, _, domain = self.email_address.rpartition("@")
        return domain.lower()


class TanssCompany(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    name: str = ""
    email: str = ""
    website: str = ""
    display_id: str = Field(alias="displayId", default="")
    inactive: bool = False


class TanssEventRule(BaseModel):
    """Eine Event-Regel (Webhook).

    ``active`` wird beim Auslösen **ignoriert** — eine auf ``false`` gesetzte Regel
    feuert trotzdem. Stilllegen geht nur per Löschen.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    name: str = ""
    active: bool = True
    assignments: list[dict] = Field(default_factory=list)
    employees: list[dict] = Field(default_factory=list)
    trigger_types: list[dict] = Field(alias="triggerTypes", default_factory=list)
    actions: list[dict] = Field(default_factory=list)

    @property
    def employee_ids(self) -> list[int]:
        return [e["employeeId"] for e in self.employees if "employeeId" in e]

    @property
    def webhook_urls(self) -> list[str]:
        return [
            action.get("params", {}).get("url", "")
            for action in self.actions
            if action.get("actionType") == "WEBHOOK"
        ]

    def targets(self, needle: str) -> bool:
        return any(needle in url for url in self.webhook_urls)


TRIGGER_TYPES = (
    "SUPPORT_CREATED", "SUPPORT_UPDATED", "SUPPORT_DELETED",
    "ABSENCE_CREATED", "ABSENCE_UPDATED", "ABSENCE_DELETED",
    "CUSTOM_ENTRY_CREATED", "CUSTOM_ENTRY_UPDATED", "CUSTOM_ENTRY_DELETED",
)
