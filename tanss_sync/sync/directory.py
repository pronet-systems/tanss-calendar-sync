"""Hält die Zuordnung TANSS-Mitarbeiter ↔ Microsoft-365-Postfach aktuell.

Das Mapping ist **kein einmaliger Setup-Schritt**. Wer in beiden Systemen neu angelegt
wird, soll ohne Handarbeit dazukommen — und wer verschwindet, soll sauber stillgelegt
werden, **ohne dass ein einziger Termin angefasst wird**.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config.models import AppConfig, UserConfig
from ..domain.identity import MatchResult, MatchStage, SyncDirection, UserMapping
from ..microsoft.repository import GraphRepository
from ..tanss.models import TanssEmployee
from ..tanss.repository import TanssRepository

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DirectoryReport:
    added: list[UserMapping] = field(default_factory=list)
    retired: list[tuple[UserMapping, str]] = field(default_factory=list)
    unresolved: list[tuple[TanssEmployee, MatchResult]] = field(default_factory=list)
    blocked: list[tuple[TanssEmployee, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.retired)

    def describe(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} neu aufgenommen")
        if self.retired:
            parts.append(f"{len(self.retired)} stillgelegt")
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} ohne Postfach")
        if self.blocked:
            parts.append(f"{len(self.blocked)} blockiert")
        return ", ".join(parts) or "keine Änderungen"


class UserDirectory:
    def __init__(self, tanss: TanssRepository, graph: GraphRepository,
                 config: AppConfig, *, foreign_sync_employees: set[int] | None = None
                 ) -> None:
        self.tanss = tanss
        self.graph = graph
        self.config = config
        # Mitarbeiter, fuer die eine fremde Terminsynchronisation laeuft.
        self.foreign_sync = foreign_sync_employees or set()

    # ------------------------------------------------------------------ Abgleich

    def refresh(self, *, dry_run: bool = False) -> DirectoryReport:
        policy = self.config.user_discovery
        report = DirectoryReport()
        if policy.mode == "off":
            return report

        employees = {e.id: e for e in self.tanss.list_technicians()}
        mapped = {u.tanss_employee_id for u in self.config.users}

        for employee in employees.values():
            if employee.id in mapped:
                continue
            self._consider(employee, report, policy)

        if policy.auto_disable_on_removal:
            self._retire_missing(employees, report)

        if report.changed and not dry_run:
            self._persist(report)
        return report

    def _consider(self, employee: TanssEmployee, report: DirectoryReport,
                  policy) -> None:
        if policy.mailbox_domains and employee.mail_domain not in {
                d.lower() for d in policy.mailbox_domains}:
            return  # Sicherheitsnetz gegen Fehlgriffe

        match = self.resolve_mailbox(employee)
        if not match.is_automatic(strict=policy.strict_matching):
            report.unresolved.append((employee, match))
            return

        mapping = UserMapping(
            tanss_employee_id=employee.id,
            tanss_name=employee.name,
            tanss_email=employee.email_address,
            mailbox=match.mailbox or "",
            graph_user_id=match.graph_user_id,
            direction=policy.default_direction,
            activated_at=activation_cutoff(self.config.sync.adopt_existing_days),
            enabled=False,
        )

        if employee.id in self.foreign_sync:
            # Nur mappen, nicht aktivieren: Zwei Systeme, die dieselben Kalender
            # abgleichen, schreiben gegeneinander.
            report.blocked.append(
                (employee, "eine andere Terminsynchronisation ist für diesen "
                           "Mitarbeiter aktiv"))
            report.added.append(mapping)
            return

        mapping.enabled = policy.mode == "map_and_enable"
        report.added.append(mapping)

    def _retire_missing(self, employees: dict[int, TanssEmployee],
                        report: DirectoryReport) -> None:
        for user in self.config.users:
            if not user.enabled:
                continue
            if user.tanss_employee_id not in employees:
                report.retired.append((self._as_mapping(user),
                                       "Mitarbeiter in TANSS nicht mehr vorhanden"))

    # ------------------------------------------------------------------ Auflösung

    def resolve_mailbox(self, employee: TanssEmployee) -> MatchResult:
        """Fünfstufig — der erste eindeutige Treffer gewinnt."""
        if not employee.email_address:
            return MatchResult(MatchStage.NONE)

        user = self.graph.resolve_user(employee.email_address)
        if user is None:
            return MatchResult(MatchStage.NONE)

        wanted = employee.email_address.lower()
        if (user.mail or "").lower() == wanted:
            stage = MatchStage.MAIL
        elif user.user_principal_name.lower() == wanted:
            stage = MatchStage.UPN
        elif wanted in user.smtp_addresses():
            stage = MatchStage.PROXY
        else:
            stage = MatchStage.NAME

        return MatchResult(stage, mailbox=user.mailbox, graph_user_id=user.id)

    # ------------------------------------------------------------------ Schreiben

    def _persist(self, report: DirectoryReport) -> None:
        by_id = {u.tanss_employee_id: u for u in self.config.users}

        for mapping in report.added:
            by_id[mapping.tanss_employee_id] = UserConfig(
                tanss_employee_id=mapping.tanss_employee_id,
                tanss_email=mapping.tanss_email,
                mailbox=mapping.mailbox,
                enabled=mapping.enabled,
                direction=mapping.direction,
                activated_at=mapping.activated_at,
            )
            log.info("Neu aufgenommen: %s (%s) → %s%s", mapping.tanss_name,
                     mapping.tanss_employee_id, mapping.mailbox,
                     "" if mapping.enabled else " [nur gemappt, nicht aktiviert]")

        for mapping, reason in report.retired:
            entry = by_id.get(mapping.tanss_employee_id)
            if entry:
                # NUR entkoppeln. Termine bleiben in beiden Systemen bestehen -
                # es gibt keinen Weg, auf dem ein Abgang zu Loeschungen fuehrt.
                entry.enabled = False
                log.warning("Stillgelegt: %s — %s. Termine bleiben unangetastet.",
                            mapping.tanss_employee_id, reason)

        self.config.users = list(by_id.values())

    @staticmethod
    def _as_mapping(user: UserConfig) -> UserMapping:
        return UserMapping(
            tanss_employee_id=user.tanss_employee_id,
            tanss_name="",
            tanss_email=user.tanss_email,
            mailbox=user.mailbox,
            enabled=user.enabled,
            direction=user.direction,
            activated_at=user.activated_at,
        )


def mappings_from_config(config: AppConfig) -> list[UserMapping]:
    return [
        UserMapping(
            tanss_employee_id=u.tanss_employee_id,
            tanss_name="",
            tanss_email=u.tanss_email,
            mailbox=u.mailbox,
            enabled=u.enabled,
            direction=SyncDirection(u.direction),
            activated_at=u.activated_at,
        )
        for u in config.users
    ]


def activation_cutoff(adopt_existing_days: int) -> datetime:
    """Ab wann Termine eines neu aktivierten Benutzers abgeglichen werden.

    Der Stichtag wirkt als serverseitiger Filter auf den **Anlagezeitpunkt**. Er
    entscheidet damit über die Umstellung:

    * ``0`` — nur was ab jetzt entsteht. Der Bestand bleibt unangetastet, wird aber
      auch nicht mehr gepflegt: Eine spätere Änderung an einem älteren Termin bliebe
      auf ihrer Seite liegen.
    * größer ``0`` — der Bestand dieser Zeitspanne wird mitgenommen. Was auf der
      Gegenseite bereits existiert, wird dabei übernommen statt verdoppelt; nur was
      dort wirklich fehlt, entsteht neu.
    """
    if adopt_existing_days <= 0:
        return datetime.now(UTC)
    return datetime.now(UTC) - timedelta(days=adopt_existing_days)
