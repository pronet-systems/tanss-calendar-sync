"""Datensätze der Zustandsdatenbank — Feld für Feld passend zum Schema."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ..domain.appointment import Appointment
from ..domain.identity import SyncDirection, SyncKey, TravelRole, UserMapping

LinkState = Literal["linked", "detached", "deleted", "ignored_pre_activation"]


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


def _epoch(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value else None


class LinkInconsistency(RuntimeError):
    """Eine Verknüpfung, die den Schreibschutz stillschweigend abschalten würde."""


@dataclass(slots=True)
class LinkRecord:
    """Die Kopplung eines Termins über beide Systeme."""

    mailbox: str
    uid: str
    sequence: int = -1
    travel_role: TravelRole = "main"
    tanss_support_id: int | None = None
    graph_event_id: str | None = None
    tanss_employee_id: int = 0
    tanss_recurrence_rule_id: int = 0
    series_master_id: str | None = None
    travel_group_id: str | None = None
    state: LinkState = "linked"
    write_direction: SyncDirection = SyncDirection.BOTH
    last_hash_tanss: str | None = None
    last_hash_graph: str | None = None
    last_written_side: str | None = None
    last_written_at: datetime | None = None
    last_seen_at: datetime | None = None

    # ---------------------------------------------------------------- Ableitung

    @classmethod
    def for_new(cls, appointment: Appointment, user: UserMapping,
                **overrides: object) -> LinkRecord:
        """Die **einzige** Stelle, an der ``write_direction`` entsteht.

        Bewusst nicht aus ``user.direction`` übernommen: Die beiden Felder heißen
        ähnlich und bedeuten Verschiedenes. Wer hier ``user.direction`` einsetzt,
        schaltet den Schreibschutz für jeden Benutzer mit dem Standard ``both`` still
        ab — ein in Outlook gelöschter Urlaubstag löschte dann den echten TANSS-Urlaub,
        ein gelöschter Anfahrt-Block über die gemeinsame Support-ID den Haupttermin.
        """
        key = appointment.key
        protected = (
            key.travel_role != "main"
            or appointment.kind.is_absence
            or not appointment.kind.syncs_to_tanss
        )
        direction = SyncDirection.TANSS_TO_M365 if protected else user.direction

        record = cls(
            mailbox=key.mailbox,
            uid=key.uid,
            sequence=key.sequence,
            travel_role=key.travel_role,
            tanss_support_id=appointment.tanss_support_id,
            graph_event_id=appointment.graph_event_id,
            tanss_employee_id=user.tanss_employee_id,
            tanss_recurrence_rule_id=appointment.recurrence_rule_id or 0,
            series_master_id=appointment.series_master_id,
            write_direction=direction,
            last_seen_at=datetime.now(UTC),
        )
        for name, value in overrides.items():
            setattr(record, name, value)
        return record

    def assert_consistent(self) -> None:
        """Zusicherung vor jedem Schreiben in die Datenbank."""
        if self.travel_role != "main" and self.write_direction is not SyncDirection.TANSS_TO_M365:
            raise LinkInconsistency(
                f"Fahrt-Zeile {self.key} trägt write_direction={self.write_direction}. "
                "Fahrt-Termine sind reine Projektionen und müssen tanss_to_m365 sein — "
                "sonst löscht ein in Outlook entfernter Fahrtblock den Haupttermin."
            )
        if self.write_direction not in tuple(SyncDirection):
            raise LinkInconsistency(f"Unbekannte write_direction: {self.write_direction!r}")

    # ---------------------------------------------------------------- Zugriff

    @property
    def key(self) -> SyncKey:
        return SyncKey(self.mailbox, self.uid, self.sequence, self.travel_role)

    @property
    def is_write_protected_in_tanss(self) -> bool:
        return self.write_direction is SyncDirection.TANSS_TO_M365

    # ---------------------------------------------------------------- Abbildung

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LinkRecord:
        return cls(
            mailbox=row["mailbox"], uid=row["uid"], sequence=row["sequence"],
            travel_role=row["travel_role"],
            tanss_support_id=row["tanss_support_id"],
            graph_event_id=row["graph_event_id"],
            tanss_employee_id=row["tanss_employee_id"],
            tanss_recurrence_rule_id=row["tanss_recurrence_rule_id"],
            series_master_id=row["series_master_id"],
            travel_group_id=row["travel_group_id"],
            state=row["state"],
            write_direction=SyncDirection(row["write_direction"]),
            last_hash_tanss=row["last_hash_tanss"],
            last_hash_graph=row["last_hash_graph"],
            last_written_side=row["last_written_side"],
            last_written_at=_ts(row["last_written_at"]),
            last_seen_at=_ts(row["last_seen_at"]),
        )

    def to_row(self) -> dict:
        return {
            "mailbox": self.mailbox, "uid": self.uid, "sequence": self.sequence,
            "travel_role": self.travel_role,
            "tanss_support_id": self.tanss_support_id,
            "graph_event_id": self.graph_event_id,
            "tanss_employee_id": self.tanss_employee_id,
            "tanss_recurrence_rule_id": self.tanss_recurrence_rule_id,
            "series_master_id": self.series_master_id,
            "travel_group_id": self.travel_group_id,
            "state": self.state,
            "write_direction": str(self.write_direction),
            "last_hash_tanss": self.last_hash_tanss,
            "last_hash_graph": self.last_hash_graph,
            "last_written_side": self.last_written_side,
            "last_written_at": _epoch(self.last_written_at),
            "last_seen_at": _epoch(self.last_seen_at),
        }


@dataclass(slots=True)
class EmergencyStop:
    id: int
    triggered_at: datetime
    scope: str
    kind: Literal["bulk_delete", "bulk_create_rebase"]
    counted: int
    threshold: int
    reason: str
    acknowledged_at: datetime | None = None
    released_at: datetime | None = None
    released_by: str | None = None

    @property
    def is_open(self) -> bool:
        return self.released_at is None

    def unacknowledged_minutes(self) -> int:
        if self.acknowledged_at:
            return 0
        return int((datetime.now(UTC) - self.triggered_at).total_seconds() // 60)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> EmergencyStop:
        return cls(
            id=row["id"], triggered_at=_ts(row["triggered_at"]),
            scope=row["scope"], kind=row["kind"], counted=row["counted"] or 0,
            threshold=row["threshold"] or 0, reason=row["reason"],
            acknowledged_at=_ts(row["acknowledged_at"]),
            released_at=_ts(row["released_at"]), released_by=row["released_by"],
        )


@dataclass(slots=True)
class RunReport:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    aborted_reason: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def touched(self) -> int:
        return self.created + self.updated + self.deleted

    def summary(self) -> str:
        parts = [f"{self.created} angelegt", f"{self.updated} geändert",
                 f"{self.deleted} gelöscht", f"{self.skipped} übergangen"]
        if self.errors:
            parts.append(f"{self.errors} Fehler")
        if self.aborted_reason:
            parts.append(f"ABGEBROCHEN: {self.aborted_reason}")
        return ", ".join(parts)
