"""Änderungen und Aktionen — was der Abgleich zu tun beschließt."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .appointment import Appointment
from .identity import SyncDirection


class SyncOperation(StrEnum):
    """Identisch mit ``audit.operation``. Beide dürfen nie auseinanderlaufen."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LINK = "link"
    RELINK = "relink"
    UNLINK = "unlink"
    DETACH = "detach"
    MOVE = "move"
    REBASE = "rebase"
    DELETION_SCHEDULED = "deletion_scheduled"
    DELETION_CANCELLED = "deletion_cancelled"
    TOKEN_ROTATE = "token_rotate"
    USER_MAP = "user_map"
    EMERGENCY_ACK = "emergency_ack"
    SKIP = "skip"

    @property
    def is_destructive(self) -> bool:
        return self is SyncOperation.DELETE


Side = StrEnum("Side", {"TANSS": "tanss", "GRAPH": "graph", "SYSTEM": "system"})
Outcome = StrEnum(
    "Outcome",
    {
        "OK": "ok",
        "FAILED": "failed",
        "BLOCKED": "blocked",
        "DRY_RUN": "dry_run",
        "SCHEDULED": "scheduled",
    },
)


@dataclass(slots=True)
class SyncAction:
    """Eine geplante Änderung. Die Begründung wandert ins Änderungsprotokoll."""

    direction: SyncDirection
    operation: SyncOperation
    appointment: Appointment
    reason: str
    changed_fields: set[str] = field(default_factory=set)

    @property
    def target_side(self) -> Side:
        """Auf welcher Seite geschrieben wird."""
        return Side.GRAPH if self.direction is SyncDirection.TANSS_TO_M365 else Side.TANSS

    def describe(self) -> str:
        """Eine Zeile für ``--dry-run``."""
        parts = [f"{self.operation.value:<10}", f"{self.target_side.value:<6}",
                 str(self.appointment.key)]
        if self.changed_fields:
            parts.append("(" + ", ".join(sorted(self.changed_fields)) + ")")
        parts.append(f"— {self.reason}")
        return " ".join(parts)


@dataclass(slots=True)
class ChangeSet:
    """Das Ergebnis eines Abgleichs für einen Benutzer."""

    actions: list[SyncAction] = field(default_factory=list)
    skipped: list[tuple[Appointment, str]] = field(default_factory=list)
    # Paare, fuer die nur die Ausgangsmarke nachgezogen wird - ohne zu schreiben.
    baselines: list[tuple[object, object]] = field(default_factory=list)

    def deletions(self) -> list[SyncAction]:
        return [a for a in self.actions if a.operation.is_destructive]

    def __len__(self) -> int:
        return len(self.actions)
