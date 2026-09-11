"""Graph-Datenmodelle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Eigene Metadaten am Termin. Nur benannte Extended Properties sind filterbar -
# Open Extensions sind es nicht, deshalb dieser Weg.
PROPERTY_GUID = "{9cf0e6f0-2d4b-4f1b-9f39-6b1a4e3b5c77}"
TANSS_ID_PROPERTY = f"String {PROPERTY_GUID} Name TanssSupportId"


class GraphUser(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    display_name: str = Field(alias="displayName", default="")
    mail: str | None = None
    user_principal_name: str = Field(alias="userPrincipalName", default="")
    proxy_addresses: list[str] = Field(alias="proxyAddresses", default_factory=list)
    given_name: str | None = Field(alias="givenName", default=None)
    surname: str | None = None
    account_enabled: bool | None = Field(alias="accountEnabled", default=None)

    @property
    def mailbox(self) -> str:
        return self.mail or self.user_principal_name

    def smtp_addresses(self) -> set[str]:
        """Alle Mailadressen inklusive Aliassen — für die Postfach-Zuordnung."""
        out = {a.lower() for a in (self.mail, self.user_principal_name) if a}
        for proxy in self.proxy_addresses:
            if proxy.lower().startswith("smtp:"):
                out.add(proxy.split(":", 1)[1].lower())
        return out


class GraphEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    ical_uid: str = Field(alias="iCalUId", default="")  # roh - erst kanonisieren!

    @field_validator("ical_uid", mode="before")
    @classmethod
    def _null_uid_is_empty(cls, value: object) -> str:
        """``iCalUId`` kommt bei einzelnen Terminen als ``null`` zurück.

        Am Kundensystem belegt: ein vom Altsystem geschriebener Termin ohne die
        Eigenschaft. Ein strenges Modell lässt daran den Abgleich des gesamten
        Postfachs scheitern — ein einzelner unvollständiger Termin darf aber nie
        alle anderen mitreißen. Fehlt die UID, ist sie eben leer; wie damit
        umzugehen ist, entscheidet :meth:`GraphRepository.uid_for`.
        """
        return value if isinstance(value, str) else ""
    subject: str = ""
    categories: list[str] = Field(default_factory=list)
    body: dict = Field(default_factory=dict)
    body_preview: str = Field(alias="bodyPreview", default="")
    start: dict = Field(default_factory=dict)
    end: dict = Field(default_factory=dict)
    location: dict = Field(default_factory=dict)
    attendees: list[dict] = Field(default_factory=list)
    organizer: dict = Field(default_factory=dict)
    is_all_day: bool = Field(alias="isAllDay", default=False)
    is_cancelled: bool = Field(alias="isCancelled", default=False)
    is_organizer: bool = Field(alias="isOrganizer", default=False)
    show_as: str = Field(alias="showAs", default="busy")
    sensitivity: str = "normal"
    response_status: dict = Field(alias="responseStatus", default_factory=dict)
    # singleInstance | occurrence | exception | seriesMaster
    type: str = "singleInstance"
    series_master_id: str | None = Field(alias="seriesMasterId", default=None)
    recurrence: dict | None = None
    online_meeting: dict | None = Field(alias="onlineMeeting", default=None)
    created: datetime | None = Field(alias="createdDateTime", default=None)
    last_modified: datetime | None = Field(alias="lastModifiedDateTime", default=None)
    single_value_extended_properties: list[dict] = Field(
        alias="singleValueExtendedProperties", default_factory=list)

    @property
    def is_series_part(self) -> bool:
        """Occurrence oder Ausnahme — die kanonische UID kommt dann vom Master."""
        return self.type in ("occurrence", "exception")

    @property
    def organizer_address(self) -> str:
        return (self.organizer.get("emailAddress", {}) or {}).get("address", "").lower()

    @property
    def teams_join_url(self) -> str | None:
        return (self.online_meeting or {}).get("joinUrl")

    @property
    def response(self) -> str:
        return self.response_status.get("response", "none")

    @property
    def tanss_support_id(self) -> int | None:
        """Unsere Kennung am Termin — nur vorhanden, wenn mitgeladen."""
        for prop in self.single_value_extended_properties:
            if prop.get("id") == TANSS_ID_PROPERTY:
                try:
                    return int(prop.get("value", ""))
                except (TypeError, ValueError):
                    return None
        return None

    def attendee_addresses(self) -> list[str]:
        return [
            (a.get("emailAddress", {}) or {}).get("address", "").lower()
            for a in self.attendees
            if (a.get("emailAddress", {}) or {}).get("address")
        ]


@dataclass(slots=True)
class DeltaResult:
    """Ergebnis eines Delta-Abrufs.

    ``removed_ids`` enthält nur IDs, **kein Datum**. Ein Termin, der lediglich aus dem
    Zeitfenster gewandert ist, erscheint dort genauso wie ein gelöschter — deshalb ist
    ein ``@removed`` allein nie ein Löschnachweis.
    """

    changed: list[GraphEvent] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    deltalink: str | None = None
    resync_required: bool = False

    def __len__(self) -> int:
        return len(self.changed) + len(self.removed_ids)
