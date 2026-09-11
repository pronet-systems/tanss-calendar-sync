"""Geführte Ersteinrichtung.

Der Leitgedanke: **Jeder Schritt prüft sein Ergebnis gegen die echte API, bevor er als
erledigt gilt.** Eine Konfiguration, die erst beim ersten Abgleich auffliegt, ist
schlimmer als gar keine — der Fehler taucht dann irgendwo im Betrieb auf, weit entfernt
von der Eingabe, die ihn verursacht hat.

Deshalb ist auch jede Fehlermeldung hier eine Anleitung: was schiefging, woran es
erfahrungsgemäß liegt und was zu tun ist. Ein nacktes „401 Unauthorized" hilft niemandem
um 23 Uhr.

Der Fortschritt wird nach jedem Schritt zwischengespeichert. Ein Abbruch — geschlossenes
Fenster, unterbrochene Verbindung — kostet damit keine bereits erledigte Arbeit.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config.models import AppConfig
from ..config.secrets import SecretRef
from ..config.store import ConfigStore

log = logging.getLogger(__name__)


def _auth_for(tanss: dict):
    """Baut die TANSS-Anmeldung aus dem bislang Eingegebenen.

    Der Token-Inhaber steht erst nach einem spaeteren Schritt fest. Bis dahin gilt 0 -
    er wird nur fuer die Erneuerung gebraucht, nicht fuer das Lesen.
    """
    from ..tanss.auth import TanssAuth

    return TanssAuth(
        SecretRef(tanss["token_ref"]),
        owner_employee_id=int(tanss.get("token_owner_employee_id") or 0),
        duration_days=int(tanss.get("token_duration_days") or 90),
    )


@dataclass(slots=True)
class StepResult:
    ok: bool
    message: str = ""
    hint: str = ""
    #: Ein Hinweis, kein Hindernis. Der Schritt hat nichts abgefragt, was sich
    #: wiederholen liesse — ein "noch einmal versuchen" liefe endlos. Gezeigt wird die
    #: Warnung trotzdem, und der Assistent fragt, ob es so weitergehen soll.
    advisory: bool = False


@dataclass(slots=True)
class SetupContext:
    """Die entstehende Konfiguration, solange sie noch unvollständig ist."""

    data: dict = field(default_factory=dict)
    store: ConfigStore | None = None
    # Was die Prüfungen nebenbei herausgefunden haben - etwa die Technikerliste.
    facts: dict = field(default_factory=dict)

    def section(self, name: str) -> dict:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        if self.store is not None:
            self.store.save_partial(self.data)


class SetupStep(ABC):
    title: str = ""
    #: Kurze Erklärung, die vor der ersten Frage steht.
    intro: str = ""

    @abstractmethod
    def prompt(self, ctx: SetupContext, ask) -> None:
        """Fragt die nötigen Angaben ab. ``ask`` kapselt die Ein-/Ausgabe."""

    @abstractmethod
    def verify(self, ctx: SetupContext) -> StepResult:
        """Prüft gegen die echte Gegenstelle. Nie nur gegen das Eingegebene."""

    def can_skip(self, ctx: SetupContext) -> bool:
        return False


# --------------------------------------------------------------------------- TANSS

class TanssUrlStep(SetupStep):
    title = "TANSS-Adresse"
    intro = ("Die Basis-Adresse der TANSS-API. Achtung: Das ist **nicht** zwingend die "
             "Adresse der Weboberfläche — bei vielen Installationen hängt die API unter "
             "einem eigenen Pfad, etwa https://tanss.example.com/backend. Zeigt die "
             "Adresse auf die Oberfläche statt auf die API, antwortet der Server auf "
             "jede Anfrage mit einem wenig hilfreichen HTTP 400.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        current = ctx.section("tanss").get("base_url", "")
        ctx.section("tanss")["base_url"] = ask.text(
            "TANSS-API-Adresse", default=current or "https://tanss.example.com/backend")

    def verify(self, ctx: SetupContext) -> StepResult:
        import httpx

        url = ctx.section("tanss")["base_url"].rstrip("/")
        try:
            response = httpx.get(f"{url}/api/tanss.x/v1/technicians", timeout=20)
        except httpx.RequestError as exc:
            return StepResult(False, f"Nicht erreichbar: {exc}",
                              "Adresse, Namensauflösung und Firewall prüfen.")

        if response.status_code in (401, 403):
            # Genau richtig: Der Endpunkt existiert und verlangt ein Token.
            return StepResult(True, "API erreichbar")
        if response.status_code == 400:
            return StepResult(
                False, "Der Server antwortet mit HTTP 400.",
                "Das ist typisch, wenn die Adresse auf die Weboberfläche zeigt statt "
                "auf die API. Häufig fehlt der Pfad /backend am Ende.")
        if response.status_code == 404:
            return StepResult(False, "Unter dieser Adresse liegt keine TANSS-API.",
                              "Pfad prüfen — erwartet wird .../api/tanss.x/v1.")
        return StepResult(True, f"API erreichbar (HTTP {response.status_code})")


class TanssTokenStep(SetupStep):
    title = "TANSS-Token"
    intro = ("Ein Dauertoken vom Typ TANSS_APP. Es wird in einer Datei abgelegt, die "
             "der Dienst auch **schreiben** können muss: Das Token erneuert sich "
             "selbst, und der Nachfolger will gespeichert werden. Eine Ablage über "
             "eine Umgebungsvariable wäre deshalb still wirkungslos — das Token liefe "
             "irgendwann ab.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        tanss = ctx.section("tanss")
        tanss["token_ref"] = ask.text(
            "Ablage des Tokens",
            default=tanss.get("token_ref", "file:/etc/tanss-calendar-sync/token"))
        token = ask.secret("TANSS-Token (Eingabe wird nicht angezeigt)")
        if token:
            ctx.facts["token"] = token.strip()

    def verify(self, ctx: SetupContext) -> StepResult:
        from ..tanss.client import TANSS_X_PREFIX, TanssClient

        tanss = ctx.section("tanss")
        ref = SecretRef(tanss["token_ref"])
        if not ref.is_writable():
            return StepResult(
                False, "Diese Ablage lässt sich nicht beschreiben.",
                "file: oder keyring: verwenden — der Dienst schreibt das erneuerte "
                "Token dorthin zurück.")

        token = ctx.facts.get("token")
        if token:
            try:
                ref.write(token)
            except Exception as exc:  # noqa: BLE001
                return StepResult(False, f"Token nicht ablegbar: {exc}",
                                  "Verzeichnis anlegen und Schreibrechte prüfen.")

        try:
            client = TanssClient(tanss["base_url"].rstrip("/"),
                                 _auth_for(tanss), timeout=20)
            technicians = client.get(f"{TANSS_X_PREFIX}/technicians") or []
            client.close()
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"Das Token wurde nicht angenommen: {exc}",
                "In TANSS ein Token vom Typ TANSS_APP ausstellen. Ein Benutzertoken "
                "reicht nicht — es läuft nach Stunden ab.")

        ctx.facts["technicians"] = technicians
        return StepResult(True, f"Token gültig, {len(technicians)} Techniker gelesen")


class TanssOwnerStep(SetupStep):
    title = "Token-Inhaber und Laufzeit"
    intro = ("Die Mitarbeiterkennung, in deren Namen Anfragen an die allgemeinen "
             "TANSS-Routen laufen. Es werden dabei genau dessen Rechte genutzt — "
             "nichts umgangen.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        tanss = ctx.section("tanss")
        technicians = ctx.facts.get("technicians") or []
        if technicians:
            ask.info("Gefundene Techniker:")
            for t in technicians[:12]:
                ask.info(f"   {t.get('id'):>6}  {t.get('name','')}")
        tanss["token_owner_employee_id"] = ask.number(
            "Mitarbeiterkennung des Token-Inhabers",
            default=tanss.get("token_owner_employee_id"))
        tanss["token_duration_days"] = ask.number(
            "Laufzeit neuer Token in Tagen", default=tanss.get("token_duration_days", 90))
        tanss["rotate_before_days"] = ask.number(
            "Erneuern, wenn weniger Tage übrig sind als",
            default=tanss.get("rotate_before_days", 60))

    def verify(self, ctx: SetupContext) -> StepResult:
        from ..tanss.client import TanssClient

        tanss = ctx.section("tanss")
        if tanss["rotate_before_days"] >= tanss["token_duration_days"]:
            return StepResult(
                False, "Die Erneuerungsschwelle liegt über der Laufzeit.",
                "Sonst gälte jedes frisch ausgestellte Token sofort wieder als "
                "erneuerungsbedürftig.")

        try:
            client = TanssClient(tanss["base_url"].rstrip("/"),
                                 _auth_for(tanss), timeout=20)
            state = client.act_as(tanss["token_owner_employee_id"]).get(
                "/api/v1/employees/ownState") or {}
            client.close()
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"Zugriff als dieser Mitarbeiter schlug fehl: {exc}",
                "Kennung prüfen. Sie muss zu einem aktiven Mitarbeiter gehören.")

        company = state.get("ownCompanyId") or 0
        if company:
            ctx.section("tanss")["own_company_id"] = company
        return StepResult(True, f"Zugriff bestätigt, eigene Firma: {company or 'unbekannt'}")


# ----------------------------------------------------------------------- Microsoft

class MicrosoftAppStep(SetupStep):
    title = "Microsoft-365-Anwendung"
    intro = ("Die in Entra registrierte Anwendung. Sie braucht die "
             "**Anwendungsberechtigungen** Calendars.ReadWrite und User.Read.All, "
             "jeweils mit erteilter Administratorzustimmung. Delegierte "
             "Berechtigungen reichen nicht: Der Dienst läuft ohne angemeldeten "
             "Benutzer.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        ms = ctx.section("microsoft")
        ms["tenant_id"] = ask.text("Verzeichnis-ID (Mandant)", default=ms.get("tenant_id"))
        ms["client_id"] = ask.text("Anwendungs-ID (Client)", default=ms.get("client_id"))

        auth = ms.setdefault("auth", {})
        mode = ask.choice("Anmeldung per", ["certificate", "secret"],
                          default=auth.get("mode", "certificate"))
        auth["mode"] = mode
        if mode == "certificate":
            auth["certificate_path"] = ask.text(
                "Pfad zur PEM-Datei", default=auth.get("certificate_path"))
            auth["thumbprint"] = ask.text(
                "Fingerabdruck des Zertifikats", default=auth.get("thumbprint"))
            auth["client_secret_ref"] = None
        else:
            ask.info("Ein Geheimnis läuft ab — meist nach 6 bis 24 Monaten. "
                     "Ein Zertifikat ist im Dauerbetrieb die ruhigere Wahl.")
            auth["client_secret_ref"] = ask.text(
                "Ablage des Geheimnisses",
                default=auth.get("client_secret_ref")
                or "file:/etc/tanss-calendar-sync/graph-secret")
            secret = ask.secret("Geheimnis (Eingabe wird nicht angezeigt)")
            if secret:
                SecretRef(auth["client_secret_ref"]).write(secret.strip())

    def verify(self, ctx: SetupContext) -> StepResult:
        from ..microsoft.auth import GraphAuth

        try:
            auth = GraphAuth(AppConfig.model_validate(
                {**ctx.data, "tanss": ctx.section("tanss")}).microsoft)
            auth.token()
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"Anmeldung fehlgeschlagen: {exc}",
                "Mandanten- und Anwendungs-ID prüfen. Bei einem Geheimnis: Es ist der "
                "**Wert**, nicht die Geheimnis-ID — beide stehen im Portal nebeneinander "
                "und werden regelmäßig verwechselt.")
        return StepResult(True, "Anmeldung erfolgreich")


class MicrosoftPermissionStep(SetupStep):
    title = "Berechtigungen und Postfachzugriff"
    intro = ("Geprüft wird nicht die Zusage im Portal, sondern der tatsächliche "
             "Zugriff auf ein echtes Postfach.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        ctx.facts["probe_mailbox"] = ask.text(
            "Postfach für die Probe (eine echte Adresse)",
            default=ctx.facts.get("probe_mailbox"))

    def verify(self, ctx: SetupContext) -> StepResult:
        from ..microsoft.auth import GraphAuth
        from ..microsoft.client import GraphClient

        mailbox = (ctx.facts.get("probe_mailbox") or "").strip()
        if not mailbox:
            return StepResult(False, "Ohne Postfach lässt sich nichts prüfen.")

        config = AppConfig.model_validate({**ctx.data, "tanss": ctx.section("tanss")})
        client = GraphClient(GraphAuth(config.microsoft),
                             timeout=config.microsoft.timeout_seconds)
        try:
            client.get(f"/users/{mailbox}", params={"$select": "id,mail"})
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"Benutzer nicht lesbar: {exc}",
                "User.Read.All als Anwendungsberechtigung erteilen und die "
                "Administratorzustimmung nicht vergessen.")

        try:
            now = datetime.now(UTC)
            client.get(f"/users/{mailbox}/calendarView", params={
                "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$top": "1"}, scope=mailbox)
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"Kalender nicht lesbar: {exc}",
                "Calendars.ReadWrite erteilen. Ist der Zugriff per App-RBAC auf "
                "ausgewählte Postfächer eingeschränkt, muss dieses Postfach in der "
                "Auswahl enthalten sein.")
        finally:
            client.close()

        return StepResult(True, f"Zugriff auf {mailbox} bestätigt")


# ------------------------------------------------------------------ Betrieb

class ForeignSyncStep(SetupStep):
    title = "Fremde Terminsynchronisation"
    intro = ("Zwei Systeme, die dieselben Kalender abgleichen, schreiben gegeneinander "
             "und erzeugen Duplikate.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        return

    def verify(self, ctx: SetupContext) -> StepResult:
        from ..sync.webhooks import WebhookManager
        from ..tanss.client import TanssClient
        from ..tanss.repository import TanssRepository

        tanss = ctx.section("tanss")
        try:
            client = TanssClient(tanss["base_url"].rstrip("/"),
                                 _auth_for(tanss), timeout=20)
            report = WebhookManager(TanssRepository(client)).inspect()
            client.close()
        except Exception as exc:  # noqa: BLE001
            return StepResult(True, f"Nicht prüfbar ({exc}) — Einrichtung läuft weiter")

        betroffen = report.foreign_employees
        ctx.facts["foreign_employees"] = sorted(betroffen)
        if not betroffen:
            return StepResult(True, "Keine fremden Meldewege gefunden")

        return StepResult(
            False,
            f"Für {len(betroffen)} Mitarbeiter laufen fremde Meldewege: "
            + ", ".join(str(e) for e in sorted(betroffen)),
            "Diese Mitarbeiter werden zunächst nur zugeordnet, aber **nicht** "
            "aktiviert. Erst dort abschalten, dann hier aktivieren: "
            "tanss-sync users enable <kennung>",
            advisory=True)


class SyncOptionsStep(SetupStep):
    title = "Abgleich"
    intro = ""

    def prompt(self, ctx: SetupContext, ask) -> None:
        sync = ctx.section("sync")
        sync["interval_seconds"] = ask.number(
            "Abstand zwischen zwei Durchläufen in Sekunden",
            default=sync.get("interval_seconds", 120))
        sync["window_days_past"] = ask.number(
            "Wie viele Tage rückwirkend abgleichen",
            default=sync.get("window_days_past", 14))
        sync["window_days_future"] = ask.number(
            "Wie viele Tage vorausschauend abgleichen",
            default=sync.get("window_days_future", 180))
        ask.info("Der nächste Wert entscheidet die Umstellung: Wie viel Bestand beim "
                 "Aktivieren eines Mitarbeiters übernommen wird. Was auf der "
                 "Gegenseite schon existiert, wird dabei übernommen statt verdoppelt. "
                 "Bei 0 bleibt der Bestand unangetastet — wird dann aber auch nicht "
                 "mehr gepflegt.")
        sync["adopt_existing_days"] = ask.number(
            "Bestand der letzten ... Tage übernehmen",
            default=sync.get("adopt_existing_days", 90))
        sync["conflict_winner"] = ask.choice(
            "Wer gewinnt, wenn beide Seiten geändert wurden",
            ["tanss", "outlook"], default=sync.get("conflict_winner", "tanss"))

    def verify(self, ctx: SetupContext) -> StepResult:
        sync = ctx.section("sync")
        if sync["interval_seconds"] < 30:
            return StepResult(False, "Ein Abstand unter 30 Sekunden ist zu eng.",
                              "Beide Gegenstellen werden bei jedem Durchlauf befragt.")
        return StepResult(True, "Übernommen")


class DryRunStep(SetupStep):
    title = "Probelauf"
    intro = ("Zum Abschluss ein Durchlauf, der **nichts schreibt**. Er zeigt, was beim "
             "ersten echten Lauf geschähe.")

    def prompt(self, ctx: SetupContext, ask) -> None:
        ctx.facts["do_dry_run"] = ask.confirm("Probelauf jetzt durchführen?", default=True)

    def can_skip(self, ctx: SetupContext) -> bool:
        return not ctx.facts.get("do_dry_run", True)

    def verify(self, ctx: SetupContext) -> StepResult:
        enabled = [u for u in ctx.data.get("users", []) if u.get("enabled")]
        if not enabled:
            return StepResult(
                True,
                "Noch kein Mitarbeiter aktiviert — es gäbe nichts abzugleichen. "
                "Später: tanss-sync users enable <kennung>")
        return StepResult(True, f"{len(enabled)} Mitarbeiter aktiviert. "
                                "Probelauf: tanss-sync sync --once --dry-run")


STEPS: list[type[SetupStep]] = [
    TanssUrlStep,
    TanssTokenStep,
    TanssOwnerStep,
    MicrosoftAppStep,
    MicrosoftPermissionStep,
    ForeignSyncStep,
    SyncOptionsStep,
    DryRunStep,
]


class SetupWizard:
    """Führt durch die Schritte und speichert nach jedem Schritt den Stand."""

    def __init__(self, store: ConfigStore, ask) -> None:
        self.store = store
        self.ask = ask

    def run(self, *, resume: bool = True) -> AppConfig | None:
        ctx = SetupContext(store=self.store)
        if resume:
            vorhanden = self.store.load_partial()
            if vorhanden:
                ctx.data = vorhanden
                self.ask.info("Angefangene Einrichtung gefunden — es geht dort weiter.")

        for index, step_class in enumerate(STEPS, start=1):
            step = step_class()
            self.ask.heading(f"Schritt {index} von {len(STEPS)}: {step.title}")
            if step.intro:
                self.ask.info(step.intro)

            while True:
                step.prompt(ctx, self.ask)
                if step.can_skip(ctx):
                    break

                result = step.verify(ctx)
                if result.ok:
                    self.ask.success(result.message or "In Ordnung")
                    break

                self.ask.failure(result.message, result.hint)

                if result.advisory:
                    # Nichts zum Wiederholen - der Schritt hat nichts abgefragt.
                    if self.ask.confirm("Trotzdem fortfahren?", default=True):
                        break
                    ctx.save()
                    return None

                if not self.ask.confirm("Noch einmal versuchen?", default=True):
                    self.ask.info("Abgebrochen. Der Stand ist gespeichert — "
                                  "weiter mit: tanss-sync setup")
                    ctx.save()
                    return None

            ctx.save()

        try:
            config = self._finish(ctx)
        except Exception as exc:  # noqa: BLE001
            # Der Zwischenstand bleibt liegen - ein erneuter Aufruf setzt dort auf,
            # statt alles noch einmal abzufragen.
            self.ask.failure(
                "Die Angaben ergeben noch keine vollständige Konfiguration.",
                f"{exc}\nDer Stand ist gespeichert — weiter mit: tanss-sync setup")
            ctx.save()
            return None

        self.store.save(config)
        self.store.clear_partial()
        return config

    def _finish(self, ctx: SetupContext) -> AppConfig:
        """Baut die endgültige Konfiguration — sie muss vollständig gültig sein."""
        data = dict(ctx.data)
        data.setdefault("version", 1)
        data.setdefault("users", [])
        return AppConfig.model_validate(data)
