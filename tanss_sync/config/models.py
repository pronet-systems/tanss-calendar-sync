"""Konfigurationsmodelle — der Stand aus Abschnitt 7 des Plans.

Jeder Block hier hat dort eine Parametertabelle. Weicht etwas ab, gilt der Plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.appointment import ServiceLocation
from ..domain.identity import SyncDirection
from .secrets import SecretRef


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TanssConfig(_Base):
    base_url: str
    token_ref: str = "file:/etc/tanss-calendar-sync/token"
    token_owner_employee_id: int
    token_duration_days: int = 90  # Entscheidung E1 - bewusst kuerzer als der Serverstandard
    rotate_before_days: int = 60
    own_company_id: int = 0
    verify_tls: bool = True
    timeout_seconds: int = 30

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/")

    @model_validator(mode="after")
    def _token_ref_must_be_writable(self) -> TanssConfig:
        if not SecretRef(self.token_ref).is_writable():
            raise ValueError(
                "tanss.token_ref muss file: oder keyring: sein — der Dienst schreibt "
                "das erneuerte Token dorthin zurück. Mit env: wäre die Rotation still "
                "wirkungslos und das Token liefe irgendwann ab."
            )
        return self


class MicrosoftAuthConfig(_Base):
    mode: Literal["certificate", "secret"] = "certificate"
    certificate_path: str | None = None
    thumbprint: str | None = None
    client_secret_ref: str | None = None

    @model_validator(mode="after")
    def _needs_matching_credential(self) -> MicrosoftAuthConfig:
        if self.mode == "certificate" and not (self.certificate_path and self.thumbprint):
            raise ValueError("auth.mode 'certificate' braucht certificate_path und thumbprint")
        if self.mode == "secret" and not self.client_secret_ref:
            raise ValueError("auth.mode 'secret' braucht client_secret_ref")
        return self


class MicrosoftConfig(_Base):
    tenant_id: str
    client_id: str
    auth: MicrosoftAuthConfig
    # Anzeige-Zeitzone der Postfaecher. Geschrieben wird an Graph grundsaetzlich UTC.
    timezone: str = "W. Europe Standard Time"
    timeout_seconds: int = 30


class SyncConfig(_Base):
    mode: Literal["poll", "push"] = "poll"
    interval_seconds: int = 120
    window_days_past: int = 14
    window_days_future: int = 180
    rebase_margin_days: int = 30
    conflict_winner: Literal["tanss", "outlook"] = "tanss"
    echo_suppression_seconds: int = 30
    lease_ttl_seconds: int = 300
    company_suffix_in_subject: bool = True
    travel_time_as_separate_events: bool = True
    sync_absences: bool = True
    ignore_show_as_free: bool = True
    ignore_all_day: bool = True
    ticket_pattern: str = r"#([0-9]{4,})"
    sync_series: bool = True
    infinite_series_end_years: int = 2
    # Wie weit rueckwirkend Bestandstermine uebernommen werden, wenn ein Benutzer
    # aktiviert wird. 0 bedeutet: nur was ab der Aktivierung entsteht.
    adopt_existing_days: int = 90
    dry_run: bool = False


class PushConfig(_Base):
    bind: str = "0.0.0.0"
    port: int = 8843
    callback_base_url: str
    path_token_ref: str


class SafetyConfig(_Base):
    alert_after_failed_runs: int = 5
    emergency_ack_after_minutes: int = 60
    # Die Mengenbremsen stehen standardmaessig auf 0, also aus. Sie hielten den Dienst
    # bei gewoehnlichen Vorgaengen an - ein zurueckgezogener Sammelurlaub, ein erster
    # Abgleich gegen einen gewachsenen Kalender -, und ein Alarm, der bei Normalbetrieb
    # schrillt, wird abgeschaltet statt beachtet.
    #
    # Was unabhaengig davon weiter traegt: Geloescht wird nur auf HTTP 404 bei gezielter
    # Einzelnachfrage, nie wegen eines Eintrags, der bloss in einer Liste fehlt. Davor
    # laeuft eine Karenzzeit, und vor jeder Loeschung wird gesichert
    # (tanss-sync deleted / restore). Abwesenheiten und Fahrt-Bloecke sind TANSS-seitig
    # ohnehin schreibgeschuetzt.
    #
    # Wer die Bremsen will, setzt sie auf einen Wert groesser 0.
    max_creates_per_run: int = 0
    max_deletes_per_run: int = 0
    max_delete_ratio: float = 0.0
    # Ab wie vielen Verknuepfungen der Anteilswert ueberhaupt gilt. Darunter sagt ein
    # Anteil nichts: Bei zwei gekoppelten Terminen sind zwei Loeschungen 100 %.
    ratio_floor: int = 20
    max_creates_per_rebase: int = 25
    deletion_requires_probe: bool = True
    deletion_grace_seconds: int = 90
    backup_retention_days: int = 90

    @model_validator(mode="after")
    def _probe_is_not_optional(self) -> SafetyConfig:
        if not self.deletion_requires_probe:
            raise ValueError(
                "deletion_requires_probe lässt sich in v1 nicht abschalten — ohne "
                "Einzelnachweis am Objekt wäre jede unvollständige Antwort ein Löschbefehl."
            )
        return self


class UserDiscoveryConfig(_Base):
    mode: Literal["map_and_enable", "map_only", "off"] = "map_and_enable"
    interval_minutes: int = 60
    strict_matching: bool = True
    auto_disable_on_removal: bool = True
    mailbox_domains: list[str] = Field(default_factory=list)
    default_direction: SyncDirection = SyncDirection.BOTH


class DefaultsConfig(_Base):
    """Vorbelegung für Termine aus Outlook — der Ersatz für das Outlook-Add-in."""

    service_location: ServiceLocation = ServiceLocation.OFFICE
    support_type_id: int = 0
    internal: bool = True


class LoggingConfig(_Base):
    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["text", "json"] = "text"
    file: str | None = "/var/log/tanss-calendar-sync/sync.log"
    rotate_mb: int = 20
    keep_files: int = 10
    audit_retention_days: int = 365
    operational_retention_days: int = 30
    redact_content: bool = False
    log_http: bool = False


class UserConfig(_Base):
    tanss_employee_id: int
    tanss_email: str
    mailbox: str
    enabled: bool = False
    direction: SyncDirection = SyncDirection.BOTH
    activated_at: datetime | None = None
    defaults: DefaultsConfig | None = None  # ueberschreibt den globalen Block


class StateConfig(_Base):
    db_path: str = "/var/lib/tanss-calendar-sync/state.db"
    lock_path: str = "/run/tanss-calendar-sync/lock"


class AppConfig(_Base):
    version: int = 1
    tanss: TanssConfig
    microsoft: MicrosoftConfig
    sync: SyncConfig = Field(default_factory=SyncConfig)
    push: PushConfig | None = None
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    user_discovery: UserDiscoveryConfig = Field(default_factory=UserDiscoveryConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    users: list[UserConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _push_needs_config(self) -> AppConfig:
        if self.sync.mode == "push" and self.push is None:
            raise ValueError(
                "sync.mode ist 'push', aber der push-Abschnitt fehlt. "
                "Ohne callback_base_url weiß TANSS nicht, wohin es melden soll."
            )
        return self

    def defaults_for(self, employee_id: int) -> DefaultsConfig:
        """Benutzer-Vorbelegung, sonst die globale."""
        for user in self.users:
            if user.tanss_employee_id == employee_id and user.defaults:
                return user.defaults
        return self.defaults

    def user(self, employee_id: int) -> UserConfig | None:
        return next((u for u in self.users if u.tanss_employee_id == employee_id), None)

    @property
    def own_mail_domains(self) -> set[str]:
        """Eigene Maildomänen — trennt interne von externen Teilnehmern."""
        domains = {d.lower() for d in self.user_discovery.mailbox_domains}
        for user in self.users:
            _, _, domain = user.mailbox.rpartition("@")
            if domain:
                domains.add(domain.lower())
        return domains
