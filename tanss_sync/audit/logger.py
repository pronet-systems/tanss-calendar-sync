"""Das Änderungsprotokoll.

Hält nicht fest, *dass* etwas passiert ist, sondern **warum**. Das ist der Unterschied
zwischen einem Protokoll, das man liest, und einem, das man wegklickt.

Der Logger wird um **jeden** Schreibvorgang gelegt, auch bei ``--dry-run`` (dann mit
``outcome='dry_run'``). Damit zeigt der Probelauf exakt das, was der echte Lauf täte —
und nicht eine zweite, möglicherweise abweichende Darstellung davon.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..domain.change import Outcome, Side, SyncAction, SyncOperation
from ..state.store import StateStore
from .redaction import Redactor

log = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, state: StateStore, redactor: Redactor, run_id: int) -> None:
        self.state = state
        self.redactor = redactor
        self.run_id = run_id

    # ------------------------------------------------------------------ Termine

    def record(self, action: SyncAction, *, outcome: Outcome | str,
               trigger: str = "poll",
               before: dict | None = None, after: dict | None = None,
               backup_id: int | None = None,
               http_status: int | None = None,
               error: str | None = None,
               duration_ms: int | None = None) -> None:
        appt = action.appointment
        self._insert(
            side=action.target_side,
            operation=action.operation,
            outcome=outcome,
            reason=action.reason,
            trigger=trigger,
            mailbox=appt.key.mailbox,
            uid=appt.key.uid,
            sequence=appt.key.sequence,
            travel_role=appt.key.travel_role,
            tanss_support_id=appt.tanss_support_id,
            graph_event_id=appt.graph_event_id,
            recurrence_rule_id=appt.recurrence_rule_id or 0,
            employee_id=appt.employee_id,
            changed_fields=sorted(action.changed_fields) or None,
            backup_id=backup_id,
            before=before, after=after,
            http_status=http_status, error=error, duration_ms=duration_ms,
        )

    def record_skip(self, appointment, reason: str, *, trigger: str = "poll") -> None:
        self._insert(
            side=Side.SYSTEM, operation=SyncOperation.SKIP, outcome=Outcome.OK,
            reason=reason, trigger=trigger,
            mailbox=appointment.key.mailbox, uid=appointment.key.uid,
            sequence=appointment.key.sequence, travel_role=appointment.key.travel_role,
            tanss_support_id=appointment.tanss_support_id,
            graph_event_id=appointment.graph_event_id,
            employee_id=appointment.employee_id,
        )

    # ------------------------------------------------------------------ System

    def record_block(self, verdict, *, scope: str) -> None:
        """Was der Not-Aus verhindert hat — der wichtigste Eintrag überhaupt."""
        self._insert(
            side=Side.SYSTEM, operation=SyncOperation.DELETE, outcome=Outcome.BLOCKED,
            reason=f"Not-Aus für {scope}: {verdict.reason}", trigger="safety",
        )

    def record_rotation(self, result, *, owner: int) -> None:
        self._insert(
            side=Side.SYSTEM, operation=SyncOperation.TOKEN_ROTATE,
            outcome=Outcome.OK if result.rotated else Outcome.FAILED,
            reason=f"{result.reason} (Inhaber {owner})",
            trigger="schedule",
            after={"expires_at": result.new_expires_at.isoformat()
                   if result.new_expires_at else None},
            error=result.error,
        )

    def record_directory(self, report) -> None:
        for mapping in report.added:
            self._insert(
                side=Side.SYSTEM, operation=SyncOperation.USER_MAP, outcome=Outcome.OK,
                reason=f"aufgenommen → {mapping.mailbox}"
                       + ("" if mapping.enabled else " (nur gemappt)"),
                trigger="discovery", employee_id=mapping.tanss_employee_id)
        for mapping, reason in report.retired:
            self._insert(
                side=Side.SYSTEM, operation=SyncOperation.USER_MAP, outcome=Outcome.OK,
                reason=f"stillgelegt: {reason} — Termine bleiben bestehen",
                trigger="discovery", employee_id=mapping.tanss_employee_id)

    def record_rebase(self, mailbox: str, *, known: int, created: int,
                      ignored: int, outcome: Outcome | str = Outcome.OK) -> None:
        self._insert(
            side=Side.GRAPH, operation=SyncOperation.REBASE, outcome=outcome,
            reason=f"Neubasierung: {known} bekannt, {created} neu, "
                   f"{ignored} als Altbestand übergangen",
            trigger="rebase", mailbox=mailbox)

    # ------------------------------------------------------------------ intern

    def _insert(self, *, side, operation, outcome, reason: str, trigger: str,
                mailbox: str | None = None, uid: str | None = None,
                sequence: int | None = None, travel_role: str | None = None,
                tanss_support_id: int | None = None, graph_event_id: str | None = None,
                recurrence_rule_id: int = 0, employee_id: int | None = None,
                changed_fields: list[str] | None = None, backup_id: int | None = None,
                before: dict | None = None, after: dict | None = None,
                http_status: int | None = None, error: str | None = None,
                duration_ms: int | None = None) -> None:
        self.state.connect().execute(
            "INSERT INTO audit (run_id, ts, side, operation, outcome, mailbox, uid, "
            "sequence, travel_role, tanss_support_id, graph_event_id, "
            "tanss_recurrence_rule_id, employee_id, reason, trigger, changed_fields, "
            "backup_id, before, after, http_status, error, duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, int(datetime.now(UTC).timestamp()),
             str(getattr(side, "value", side)), str(getattr(operation, "value", operation)),
             str(getattr(outcome, "value", outcome)),
             mailbox, uid, sequence, travel_role, tanss_support_id, graph_event_id,
             recurrence_rule_id, employee_id,
             self.redactor.scrub(reason), trigger,
             json.dumps(changed_fields, ensure_ascii=False) if changed_fields else None,
             backup_id,
             _json(self.redactor.snapshot(before)),
             _json(self.redactor.snapshot(after)),
             http_status, self.redactor.scrub(error) if error else None, duration_ms),
        )


def _json(value: dict | None) -> str | None:
    return json.dumps(value, ensure_ascii=False, default=str) if value else None
