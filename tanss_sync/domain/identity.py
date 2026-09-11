"""Identität: Sync-Schlüssel, Richtungen und die Zuordnung Mitarbeiter ↔ Postfach."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from .uid import NO_SEQUENCE

TravelRole = Literal["main", "travel_to", "travel_back"]


class SyncDirection(StrEnum):
    BOTH = "both"
    TANSS_TO_M365 = "tanss_to_m365"
    M365_TO_TANSS = "m365_to_tanss"

    def allows_to_tanss(self) -> bool:
        return self in (SyncDirection.BOTH, SyncDirection.M365_TO_TANSS)

    def allows_to_m365(self) -> bool:
        return self in (SyncDirection.BOTH, SyncDirection.TANSS_TO_M365)


@dataclass(frozen=True, slots=True)
class SyncKey:
    """Identifiziert ein Termin-Paar über beide Systeme hinweg.

    Feld für Feld identisch mit dem Primärschlüssel der Tabelle ``links``.
    Weicht eines ab, findet der Abgleich seine eigenen Verknüpfungen nicht wieder.
    """

    mailbox: str
    uid: str
    sequence: int = NO_SEQUENCE
    travel_role: TravelRole = "main"

    @property
    def is_occurrence(self) -> bool:
        return self.sequence >= 0

    @property
    def is_travel(self) -> bool:
        return self.travel_role != "main"

    def with_travel(self, role: TravelRole) -> SyncKey:
        return SyncKey(self.mailbox, self.uid, self.sequence, role)

    def __str__(self) -> str:  # fuer Logzeilen
        parts = [self.mailbox, self.uid[:24]]
        if self.is_occurrence:
            parts.append(f"seq={self.sequence}")
        if self.is_travel:
            parts.append(self.travel_role)
        return " ".join(parts)


@dataclass(slots=True)
class UserMapping:
    """Ein TANSS-Mitarbeiter und sein Microsoft-365-Postfach."""

    tanss_employee_id: int
    tanss_name: str
    tanss_email: str
    mailbox: str
    graph_user_id: str | None = None
    enabled: bool = False
    direction: SyncDirection = SyncDirection.BOTH
    activated_at: datetime | None = None

    @property
    def is_ready(self) -> bool:
        """Synchronisierbar nur mit Postfach, Aktivierung und Freigabe."""
        return bool(self.enabled and self.mailbox and self.activated_at)


class MatchStage(StrEnum):
    """Wie sicher eine Postfach-Zuordnung ist — siehe Abschnitt 4.1.1."""

    MAIL = "mail"
    UPN = "upn"
    PROXY = "proxy"
    NAME = "name"
    AMBIGUOUS = "ambiguous"
    NONE = "none"

    @property
    def is_unique(self) -> bool:
        """Stufen 1-3 sind eindeutig und dürfen automatisch übernommen werden."""
        return self in (MatchStage.MAIL, MatchStage.UPN, MatchStage.PROXY)


@dataclass(slots=True)
class MatchResult:
    stage: MatchStage
    mailbox: str | None = None
    graph_user_id: str | None = None
    candidates: list[str] | None = None

    def is_automatic(self, *, strict: bool) -> bool:
        """Darf ohne Rückfrage übernommen werden?

        Bei ``strict`` zählt nur eine eindeutige Mail-Übereinstimmung. Ohne ``strict``
        wird zusätzlich der Namensabgleich zugelassen — bequemer, aber fehleranfälliger.
        """
        if self.stage.is_unique:
            return True
        return self.stage is MatchStage.NAME and not strict
