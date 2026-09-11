"""Diagnose aller Voraussetzungen.

``doctor`` ruft dieselben Prüfungen auf wie der Einrichtungsassistent — Prüfung bei der
Einrichtung und Diagnose im Betrieb sind derselbe Code. Was im Setup funktioniert hat,
muss hier genauso funktionieren, sonst hat sich etwas verändert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..config.models import AppConfig
from ..config.secrets import SecretRef
from ..microsoft.repository import GraphRepository
from ..state.store import StateStore
from ..tanss.auth import TanssAuth
from ..tanss.repository import TanssRepository
from ..util.timezone import TimeConverter


class Level(StrEnum):
    OK = "ok"
    WARN = "warnung"
    FAIL = "fehler"


@dataclass(slots=True)
class Check:
    name: str
    level: Level
    message: str
    hint: str | None = None


@dataclass(slots=True)
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: Level, message: str, hint: str | None = None) -> None:
        self.checks.append(Check(name, level, message, hint))

    @property
    def worst(self) -> Level:
        if any(c.level is Level.FAIL for c in self.checks):
            return Level.FAIL
        if any(c.level is Level.WARN for c in self.checks):
            return Level.WARN
        return Level.OK

    @property
    def exit_code(self) -> int:
        return {Level.OK: 0, Level.WARN: 1, Level.FAIL: 2}[self.worst]


class Doctor:
    def __init__(self, config: AppConfig, *, tanss: TanssRepository | None = None,
                 auth: TanssAuth | None = None, graph: GraphRepository | None = None,
                 store: StateStore | None = None) -> None:
        self.config = config
        self.tanss = tanss
        self.auth = auth
        self.graph = graph
        self.store = store

    def run(self) -> Diagnosis:
        d = Diagnosis()
        self._check_secrets(d)
        self._check_tanss(d)
        self._check_token(d)
        self._check_graph(d)
        self._check_timezone(d)
        self._check_state(d)
        self._check_users(d)
        return d

    # ------------------------------------------------------------------ Prüfungen

    def _check_secrets(self, d: Diagnosis) -> None:
        ref = SecretRef(self.config.tanss.token_ref)
        if not ref.is_writable():
            d.add("Token-Ablage", Level.FAIL,
                  f"{ref.raw} ist nicht beschreibbar",
                  "Die Selbstrotation braucht file: oder keyring:. Mit env: liefe das "
                  "Token irgendwann ab, ohne dass jemand etwas merkt.")
            return
        problems = ref.check_permissions()
        if problems:
            d.add("Dateirechte", Level.WARN, "; ".join(problems),
                  "chmod 0600 auf die Token-Datei, Eigentümer der Dienstbenutzer.")
        else:
            d.add("Dateirechte", Level.OK, "Token-Datei ist ausreichend geschützt")

    def _check_tanss(self, d: Diagnosis) -> None:
        if self.tanss is None:
            return
        try:
            techs = self.tanss.list_technicians()
        except Exception as exc:  # noqa: BLE001 - Diagnose soll alles melden
            d.add("TANSS-Verbindung", Level.FAIL, str(exc),
                  "Basisadresse und Token prüfen. Die URL steht in TANSS unter "
                  "Administration → API-Konfiguration → Backend-API-URL.")
            return

        without_mail = [t for t in techs if not t.email_address]
        d.add("TANSS-Verbindung", Level.OK, f"{len(techs)} Techniker erreichbar")
        if without_mail:
            names = ", ".join(t.name for t in without_mail[:5])
            d.add("Mitarbeiter-Mailadressen", Level.WARN,
                  f"{len(without_mail)} ohne E-Mail: {names}",
                  "Ohne Mailadresse ist keine Postfach-Zuordnung möglich.")

    def _check_token(self, d: Diagnosis) -> None:
        if self.auth is None:
            return
        warning = self.auth.warning()
        remaining = self.auth.days_remaining()
        if warning and remaining is not None and remaining < 0:
            d.add("Token", Level.FAIL, warning,
                  "Ein abgelaufenes Token kann sich nicht selbst erneuern. "
                  "Neues Token hinterlegen: tanss-sync token set")
        elif warning:
            d.add("Token", Level.WARN, warning)
        else:
            d.add("Token", Level.OK,
                  f"gültig bis {self.auth.expires_at():%d.%m.%Y} "
                  f"({remaining} Tage)")

        if self.tanss is not None:
            try:
                ok = self.auth.can_rotate(self.tanss.client)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                d.add("Token-Erneuerung", Level.OK,
                      f"Trockentest bestanden (als Mitarbeiter "
                      f"{self.auth.owner_employee_id})")
            else:
                d.add("Token-Erneuerung", Level.FAIL,
                      "Trockentest fehlgeschlagen",
                      f"Mitarbeiter {self.auth.owner_employee_id} braucht das Recht "
                      "„Administration: System API-Tokens für ext. Anbindungen und "
                      "Schnittstellen erzeugen" "und muss aktiv sein. Ohne das läuft "
                      "das Token irgendwann ersatzlos ab.")

    def _check_graph(self, d: Diagnosis) -> None:
        if self.graph is None:
            return
        sample = next((u for u in self.config.users if u.enabled), None)
        if sample is None:
            d.add("Microsoft 365", Level.WARN, "kein aktivierter Benutzer zum Prüfen")
            return
        try:
            user = self.graph.resolve_user(sample.mailbox)
        except Exception as exc:  # noqa: BLE001
            d.add("Microsoft 365", Level.FAIL, str(exc),
                  "Berechtigungen prüfen: Calendars.ReadWrite und User.Read.All, "
                  "jeweils als Anwendungsberechtigung mit Administratorzustimmung.")
            return
        if user is None:
            d.add("Microsoft 365", Level.FAIL,
                  f"Postfach {sample.mailbox} nicht auffindbar",
                  "Stimmt die Adresse? Ohne User.Read.All liefert die Suche nichts.")
        else:
            d.add("Microsoft 365", Level.OK,
                  f"Postfach {user.mailbox} erreichbar")

    def _check_timezone(self, d: Diagnosis) -> None:
        converter = TimeConverter(self.config.microsoft.timezone)
        if converter.is_known_timezone():
            d.add("Zeitzone", Level.OK, self.config.microsoft.timezone)
        else:
            d.add("Zeitzone", Level.WARN,
                  f"{self.config.microsoft.timezone} ist nicht auflösbar",
                  "Wirkt nur auf die Anzeige — geschrieben wird ohnehin UTC.")

    def _check_state(self, d: Diagnosis) -> None:
        if self.store is None:
            return
        try:
            self.store.migrate()
            healthy = self.store.integrity_check()
        except Exception as exc:  # noqa: BLE001
            d.add("Zustandsdatenbank", Level.FAIL, str(exc))
            return
        if not healthy:
            d.add("Zustandsdatenbank", Level.FAIL, "Integritätsprüfung fehlgeschlagen",
                  "Aus der Sicherung zurückspielen: tanss-sync db backup / restore.")
            return

        stats = self.store.stats()
        d.add("Zustandsdatenbank", Level.OK,
              f"{stats['links']} Verknüpfungen, {stats['audit']} Protokolleinträge")

        stop = self.store.active_emergency()
        if stop:
            minutes = stop.unacknowledged_minutes()
            level = (Level.FAIL
                     if minutes > self.config.safety.emergency_ack_after_minutes
                     else Level.WARN)
            d.add("Not-Aus", level,
                  f"{stop.kind} für {stop.scope} seit {stop.triggered_at:%d.%m. %H:%M} "
                  f"({stop.counted} von {stop.threshold})",
                  "Freigeben mit: tanss-sync sync --once --allow-bulk-delete. "
                  "tanss-sync ack beendet nur den Alarm, hebt die Sperre nicht auf.")

        failed = self.store.consecutive_failed_runs()
        if failed >= self.config.safety.alert_after_failed_runs:
            d.add("Läufe", Level.FAIL, f"{failed} Läufe in Folge gescheitert")

    def _check_users(self, d: Diagnosis) -> None:
        enabled = [u for u in self.config.users if u.enabled]
        without_activation = [u for u in enabled if not u.activated_at]
        if not self.config.users:
            d.add("Benutzer", Level.WARN, "keine Zuordnungen hinterlegt",
                  "tanss-sync users discover")
        elif without_activation:
            d.add("Benutzer", Level.FAIL,
                  f"{len(without_activation)} aktiviert ohne Aktivierungszeitpunkt",
                  "Ohne ihn ist nicht entscheidbar, welche Termine Altbestand sind — "
                  "der erste Lauf würde alles übertragen.")
        else:
            d.add("Benutzer", Level.OK,
                  f"{len(enabled)} von {len(self.config.users)} aktiviert")
