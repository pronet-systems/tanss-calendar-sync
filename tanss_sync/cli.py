"""Kommandozeile.

Jedes Kommando ist entweder **lesend** oder **schreibend**. Schreibende nehmen die
Prozess-Sperre und brechen ab, wenn ein anderer Lauf sie hält.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config.store import ConfigError, ConfigStore
from .runtime import open_runtime, write_lock
from .setup.doctor import Doctor, Level
from .sync.directory import UserDirectory

app = typer.Typer(
    name="tanss-sync",
    help="Bidirektionaler Terminabgleich zwischen TANSS und Microsoft 365.",
    no_args_is_help=True,
    add_completion=False,
)
users_app = typer.Typer(help="Mitarbeiter und Postfächer.", no_args_is_help=True)
token_app = typer.Typer(help="Das TANSS-Dauertoken.", no_args_is_help=True)
db_app = typer.Typer(help="Zustandsdatenbank.", no_args_is_help=True)
webhooks_app = typer.Typer(help="TANSS-Event-Regeln (Meldewege).",
                           no_args_is_help=True)

log = logging.getLogger(__name__)
app.add_typer(users_app, name="users")
app.add_typer(token_app, name="token")
app.add_typer(db_app, name="db")
app.add_typer(webhooks_app, name="webhooks")

console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[str | None, typer.Option("--config", "-c",
                                               help="Pfad zur config.json")]
DryRun = Annotated[bool, typer.Option("--dry-run",
                                      help="Nichts schreiben, nur zeigen")]

_LEVEL_STYLE = {Level.OK: "green", Level.WARN: "yellow", Level.FAIL: "red"}
_LEVEL_MARK = {Level.OK: "OK", Level.WARN: "!", Level.FAIL: "X"}


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False):
    if version:
        console.print(f"tanss-sync {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------- Diagnose

@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Alle Voraussetzungen prüfen. Lesend."""
    with _runtime(config) as rt:
        diagnosis = Doctor(rt.config, tanss=rt.tanss, auth=rt.auth,
                           graph=rt.graph, store=rt.state).run()

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=3)
    table.add_column("Prüfung", style="bold")
    table.add_column("Ergebnis")
    for check in diagnosis.checks:
        style = _LEVEL_STYLE[check.level]
        table.add_row(f"[{style}]{_LEVEL_MARK[check.level]}[/{style}]",
                      check.name, check.message)
    console.print(table)

    hints = [c for c in diagnosis.checks if c.hint and c.level is not Level.OK]
    if hints:
        console.print("\n[bold]Was zu tun ist[/bold]")
        for check in hints:
            console.print(f"  [{_LEVEL_STYLE[check.level]}]{check.name}[/]: {check.hint}")

    raise typer.Exit(diagnosis.exit_code)


@app.command()
def health(config: ConfigOpt = None) -> None:
    """Kurzstatus mit Exit-Code — für die Überwachung. Lesend.

    0 gesund · 1 Warnung · 2 gestört
    """
    with _runtime(config) as rt:
        diagnosis = Doctor(rt.config, tanss=rt.tanss, auth=rt.auth,
                           graph=rt.graph, store=rt.state).run()
    worst = [c for c in diagnosis.checks if c.level is diagnosis.worst]
    console.print(f"{diagnosis.worst.value}: " +
                  "; ".join(f"{c.name} — {c.message}" for c in worst[:3]))
    raise typer.Exit(diagnosis.exit_code)


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Letzter Lauf, Not-Aus, Bestandszahlen. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        last = rt.state.last_run()
        stats = rt.state.stats()
        stop = rt.state.active_emergency()

        console.print(f"[bold]Konfiguration[/bold]  {rt.store.path}")
        console.print(f"[bold]Betriebsart[/bold]    {rt.config.sync.mode}"
                      f"  (alle {rt.config.sync.interval_seconds}s)")

        enabled = sum(1 for u in rt.config.users if u.enabled)
        console.print(f"[bold]Benutzer[/bold]       {enabled} von "
                      f"{len(rt.config.users)} aktiviert")
        console.print(f"[bold]Verknüpfungen[/bold]  {stats['links']}")

        if last:
            state = "läuft" if not last["finished_at"] else "beendet"
            console.print(f"[bold]Letzter Lauf[/bold]   #{last['id']} ({state}): "
                          f"{last['created']} angelegt, {last['updated']} geändert, "
                          f"{last['deleted']} gelöscht, {last['errors']} Fehler")
            if last["aborted_reason"]:
                console.print(f"  [red]abgebrochen:[/red] {last['aborted_reason']}")
        else:
            console.print("[bold]Letzter Lauf[/bold]   noch keiner")

        if stop:
            console.print(f"\n[red bold]Not-Aus aktiv[/red bold] — {stop.kind} für "
                          f"{stop.scope}, seit {stop.triggered_at:%d.%m. %H:%M}")
            console.print(f"  {stop.reason}")
            console.print("  Freigeben: [bold]tanss-sync sync --once "
                          "--allow-bulk-delete[/bold]")
            if not stop.acknowledged_at:
                console.print("  Alarm stummschalten (ohne Freigabe): "
                              "[bold]tanss-sync ack[/bold]")


@app.command()
def ack(config: ConfigOpt = None) -> None:
    """Not-Aus quittieren. Beendet den Alarm — **hebt die Sperre nicht auf.**"""
    with _runtime(config, with_graph=False) as rt, write_lock(rt.config, "ack"):
        stop = rt.state.active_emergency()
        if not stop:
            console.print("Kein aktiver Not-Aus.")
            raise typer.Exit()
        rt.state.ack_emergency(stop.id)
        console.print(f"Quittiert: {stop.kind} für {stop.scope}.")
        console.print("[yellow]Die Sperre bleibt bestehen.[/yellow] Freigeben nur mit "
                      "[bold]tanss-sync sync --once --allow-bulk-delete[/bold].")


# ---------------------------------------------------------------------- Abgleich

@app.command("sync")
def sync_cmd(
    config: ConfigOpt = None,
    once: Annotated[bool, typer.Option("--once", help="Ein Durchlauf statt Dauerbetrieb")] = True,
    dry_run: DryRun = False,
    allow_bulk_delete: Annotated[bool, typer.Option(
        "--allow-bulk-delete",
        help="Einen ausgelösten Not-Aus freigeben und die Löschungen ausführen")] = False,
    allow_bulk_create: Annotated[bool, typer.Option(
        "--allow-bulk-create",
        help="Viele Neuanlagen in einem Lauf zulassen (erster Abgleich)")] = False,
) -> None:
    """Einen Abgleich durchführen. Mit --dry-run lesend, sonst schreibend."""
    from .sync.engine import SyncEngine

    with _runtime(config) as rt:
        if rt.graph is None:
            err.print("[red]Ohne Microsoft-Zugang ist kein Abgleich möglich.[/red]")
            raise typer.Exit(2)

        def run() -> None:
            engine = SyncEngine(rt.config, rt.tanss, rt.graph, rt.state)
            report = engine.run_once(dry_run=dry_run,
                                     allow_bulk_delete=allow_bulk_delete,
                                     allow_bulk_create=allow_bulk_create)

            if dry_run:
                console.print("[bold]Probelauf — es wurde nichts geschrieben.[/bold]")
                console.print()
            _print_actions(rt.state, run_id=report.run_id)
            console.print()
            console.print(f"[bold]{report.summary()}[/bold]")
            if report.aborted_reason:
                console.print(f"[red]{report.aborted_reason}[/red]")
                flag = ("--allow-bulk-create" if "Neuanlagen" in report.aborted_reason
                        else "--allow-bulk-delete")
                console.print(f"Freigeben mit: [bold]tanss-sync sync --once {flag}[/bold]")
                console.print("[dim]Vorher unbedingt mit --dry-run ansehen.[/dim]")
                raise typer.Exit(2)

        if dry_run:
            run()
        else:
            with write_lock(rt.config, "sync"):
                run()


@app.command("setup")
def setup_cmd(
    config: ConfigOpt = None,
    restart: Annotated[bool, typer.Option(
        "--restart", help="Angefangene Einrichtung verwerfen und von vorn beginnen"
    )] = False,
) -> None:
    """Geführte Ersteinrichtung. Schreibend.

    Jeder Schritt prüft sein Ergebnis gegen die echte Gegenstelle, bevor er als
    erledigt gilt — eine Konfiguration, die erst im Betrieb auffliegt, ist schlimmer
    als gar keine. Der Fortschritt wird nach jedem Schritt gespeichert; ein Abbruch
    kostet keine bereits erledigte Arbeit.
    """
    from .setup.prompts import Asker
    from .setup.wizard import SetupWizard

    store = ConfigStore.discover(config)
    if store.exists():
        console.print(f"Es gibt bereits eine Konfiguration: [bold]{store.path}[/bold]")
        if not typer.confirm("Wirklich neu einrichten und sie überschreiben?",
                             default=False):
            raise typer.Exit(0)
    if restart:
        store.clear_partial()

    console.print("[bold]Einrichtung des Terminabgleichs[/bold]")
    result = SetupWizard(store, Asker()).run()
    if result is None:
        raise typer.Exit(1)

    console.print()
    console.print(f"[green]Fertig.[/green] Konfiguration: [bold]{store.path}[/bold]")
    console.print("Nächste Schritte:")
    console.print("  1. [bold]tanss-sync users discover[/bold] — Postfächer zuordnen")
    console.print("  2. [bold]tanss-sync users enable <kennung>[/bold] — mit **einem** "
                  "Mitarbeiter beginnen")
    console.print("  3. [bold]tanss-sync sync --once --dry-run[/bold] — ansehen, was "
                  "geschähe")
    console.print("  4. [bold]tanss-sync doctor[/bold] — alles noch einmal prüfen")


@app.command("run")
def run_cmd(config: ConfigOpt = None) -> None:
    """Dauerbetrieb — das Kommando, das der Systemdienst ausführt. Schreibend.

    Läuft, bis der Dienst beendet wird. Ein ``SIGTERM`` aus ``systemctl stop`` wird
    abgewartet: Der laufende Durchlauf wird zu Ende geführt, statt mitten in einem
    Schreibvorgang abzubrechen.

    Ein Fehler in einem Durchlauf beendet den Dienst **nicht**. Eine TANSS-Instanz im
    Wartungsfenster oder ein kurzer Netzausfall darf keinen Neustart von Hand
    erfordern; sichtbar wird beides über ``health`` und das Protokoll.
    """
    import signal

    from .sync.engine import SyncEngine

    stopping = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        log.info("Signal %s empfangen — der laufende Durchlauf wird beendet", signum)
        stopping.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    with _runtime(config) as rt:
        if rt.graph is None:
            err.print("[red]Ohne Microsoft-Zugang ist kein Abgleich möglich.[/red]")
            raise typer.Exit(2)

        interval = rt.config.sync.interval_seconds
        console.print(f"[bold]Dauerbetrieb gestartet[/bold] — Abgleich alle "
                      f"{interval} s. Beenden mit Strg-C.")
        if rt.config.sync.dry_run:
            console.print("[yellow]dry_run steht in der Konfiguration — "
                          "es wird nichts geschrieben.[/yellow]")

        engine = SyncEngine(rt.config, rt.tanss, rt.graph, rt.state)
        last_discovery = 0.0
        discovery_every = rt.config.user_discovery.interval_minutes * 60

        with write_lock(rt.config, "run"):
            while not stopping.is_set():
                started = time.monotonic()
                try:
                    _maybe_rotate_token(rt)
                    if (rt.config.user_discovery.mode != "off"
                            and started - last_discovery >= discovery_every):
                        last_discovery = started
                        _run_discovery(rt)

                    report = engine.run_once()
                    if report.aborted_reason:
                        log.error("Durchlauf angehalten: %s", report.aborted_reason)
                    else:
                        log.info("Durchlauf beendet: %s", report.summary())
                except Exception:
                    # Ein Fehler beendet den Dienst nicht - siehe Beschreibung oben.
                    log.exception("Durchlauf gescheitert")

                # Die Wartezeit zaehlt ab dem Start, nicht ab dem Ende: Sonst
                # verschoebe sich der Takt mit jeder langen Runde weiter nach hinten.
                rest = max(1.0, interval - (time.monotonic() - started))
                stopping.wait(rest)

        console.print("[bold]Dauerbetrieb beendet.[/bold]")


def _maybe_rotate_token(rt) -> None:
    """Erneuert das TANSS-Token, bevor es abläuft.

    Das Token stellt seinen eigenen Nachfolger aus — Zugangsdaten braucht es dafür
    nicht. Bleibt die Erneuerung aus, steht der Dienst irgendwann ohne Vorwarnung
    still. Das neue Token wird gegengetestet; besteht es den Test nicht, bleibt das
    alte aktiv, denn es ist ja noch gültig.
    """
    try:
        result = rt.auth.rotate_if_needed(
            rt.tanss.client,
            before_days=rt.config.tanss.rotate_before_days,
            verify=lambda candidate: _token_works(rt, candidate))
        if result.rotated:
            log.info("TANSS-Token erneuert, gültig bis %s", result.new_expires_at)
        elif result.error:
            log.warning("Token nicht erneuert: %s (%s)", result.reason, result.error)
    except Exception as exc:  # noqa: BLE001
        log.warning("Token-Erneuerung fehlgeschlagen: %s", exc)


def _token_works(rt, candidate: str) -> bool:
    """Gegentest mit einem eigenen Client — das laufende Token bleibt unangetastet."""
    from .tanss.client import TANSS_X_PREFIX, TanssClient

    probe = TanssClient(rt.config.tanss.api_base, _FixedToken(candidate),
                        timeout=rt.config.tanss.timeout_seconds,
                        verify_tls=rt.config.tanss.verify_tls)
    try:
        probe.get(f"{TANSS_X_PREFIX}/technicians")
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        probe.close()


def _run_discovery(rt) -> None:
    try:
        directory = UserDirectory(rt.tanss, rt.graph, rt.config,
                                  foreign_sync_employees=_foreign_sync_employees(rt))
        report = directory.refresh(dry_run=False)
        if report.added or report.retired:
            rt.store.save(rt.config)
            log.info("Verzeichnisabgleich: %s", report.describe())
    except Exception as exc:  # noqa: BLE001
        log.warning("Verzeichnisabgleich fehlgeschlagen: %s", exc)


def _print_actions(state, limit: int = 60, *, run_id: int | None = None) -> None:
    """Zeigt, was **dieser** Lauf getan hätte oder getan hat.

    ``run_id`` ist wichtig: Ohne ihn zeigte ein Lauf ohne Änderungen die Tabelle des
    vorherigen Laufs, weil dessen Einträge dann die jüngsten in der Tabelle sind. Es
    sähe so aus, als wäre gerade etwas geschrieben worden.
    """
    if run_id is None:
        run_id = (state.connect().execute(
            "SELECT MAX(run_id) AS n FROM audit").fetchone() or {"n": None})["n"]

    rows = state.connect().execute(
        "SELECT operation, outcome, reason, mailbox, uid, changed_fields "
        "FROM audit WHERE run_id = ? AND side != 'system' ORDER BY id LIMIT ?",
        (run_id, limit)).fetchall()
    if not rows:
        console.print("[dim]Keine Änderungen.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Was")
    table.add_column("Ergebnis")
    table.add_column("Termin")
    table.add_column("Begründung")
    for row in rows:
        style = {"ok": "green", "dry_run": "cyan", "failed": "red",
                 "blocked": "yellow"}.get(row["outcome"], "")
        uid = (row["uid"] or "")[:26]
        fields = f" ({row['changed_fields']})" if row["changed_fields"] else ""
        table.add_row(row["operation"],
                      f"[{style}]{row['outcome']}[/{style}]" if style else row["outcome"],
                      uid, (row["reason"] or "") + fields)
    console.print(table)


# ---------------------------------------------------------------------- Benutzer

@users_app.command("list")
def users_list(config: ConfigOpt = None) -> None:
    """Mitarbeiter, Postfächer und Status. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        technicians = rt.tanss.list_technicians()
        mapped = {u.tanss_employee_id: u for u in rt.config.users}

        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", justify="right")
        table.add_column("Mitarbeiter")
        table.add_column("TANSS-Mail")
        table.add_column("Postfach")
        table.add_column("Status")

        for tech in technicians:
            entry = mapped.get(tech.id)
            if entry is None:
                state = "[dim]nicht zugeordnet[/dim]"
                mailbox = "—"
            elif entry.enabled:
                state = "[green]aktiv[/green]"
                mailbox = entry.mailbox
            else:
                state = "[yellow]gemappt, inaktiv[/yellow]"
                mailbox = entry.mailbox
            table.add_row(str(tech.id), tech.name, tech.email_address, mailbox, state)

        console.print(table)


@users_app.command("discover")
def users_discover(config: ConfigOpt = None, dry_run: DryRun = False) -> None:
    """Verzeichnisabgleich ausführen. Schreibend (außer mit --dry-run)."""
    with _runtime(config) as rt:
        if rt.graph is None:
            err.print("[red]Ohne Microsoft-Zugang ist kein Abgleich möglich.[/red]")
            raise typer.Exit(2)

        def run() -> None:
            foreign = _foreign_sync_employees(rt)
            directory = UserDirectory(rt.tanss, rt.graph, rt.config,
                                      foreign_sync_employees=foreign)
            report = directory.refresh(dry_run=dry_run)

            console.print(f"[bold]{report.describe()}[/bold]")
            for mapping in report.added:
                mark = "aktiviert" if mapping.enabled else "nur gemappt"
                console.print(f"  + {mapping.tanss_employee_id:>5} "
                              f"{mapping.tanss_email} → {mapping.mailbox} [{mark}]")
            for mapping, reason in report.retired:
                console.print(f"  - {mapping.tanss_employee_id:>5} stillgelegt: {reason}")
                console.print("    [dim]Termine bleiben in beiden Systemen bestehen.[/dim]")
            for employee, reason in report.blocked:
                console.print(f"  [yellow]![/yellow] {employee.id:>5} {employee.name}: "
                              f"{reason}")
            for employee, match in report.unresolved:
                console.print(f"  [dim]?[/dim] {employee.id:>5} {employee.name}: "
                              f"kein eindeutiges Postfach ({match.stage.value})")

            if not dry_run and report.changed:
                rt.store.save(rt.config)
                console.print(f"\nKonfiguration gespeichert: {rt.store.path}")
            elif dry_run and report.changed:
                console.print("\n[dim]--dry-run: nichts gespeichert.[/dim]")

        if dry_run:
            run()
        else:
            with write_lock(rt.config, "users discover"):
                run()


@users_app.command("enable")
def users_enable(
    employee_id: int,
    config: ConfigOpt = None,
    force_parallel: Annotated[bool, typer.Option(
        "--force-parallel",
        help="Trotz aktiver fremder Terminsynchronisation aktivieren")] = False,
) -> None:
    """Benutzer aktivieren. Schreibend."""
    with _runtime(config, with_graph=False) as rt, write_lock(rt.config, "users enable"):
        entry = rt.config.user(employee_id)
        if entry is None:
            err.print(f"[red]{employee_id} ist nicht zugeordnet.[/red] "
                      "Erst: tanss-sync users discover")
            raise typer.Exit(2)

        if employee_id in _foreign_sync_employees(rt) and not force_parallel:
            err.print(f"[red]Für {employee_id} läuft eine andere Terminsynchronisation."
                      "[/red]\nZwei Systeme, die dieselben Kalender abgleichen, "
                      "schreiben gegeneinander und erzeugen Duplikate.\n"
                      "Erst dort abschalten — oder bewusst übersteuern mit "
                      "[bold]--force-parallel[/bold].")
            raise typer.Exit(2)

        entry.enabled = True
        if entry.activated_at is None:
            from .sync.directory import activation_cutoff

            days = rt.config.sync.adopt_existing_days
            entry.activated_at = activation_cutoff(days)
            if days > 0:
                console.print(
                    f"Aktivierungszeitpunkt gesetzt: {entry.activated_at:%d.%m.%Y %H:%M}"
                    f" — der Bestand der letzten {days} Tage wird übernommen, "
                    "alles Ältere bleibt unangetastet.")
            else:
                console.print(
                    f"Aktivierungszeitpunkt gesetzt: {entry.activated_at:%d.%m.%Y %H:%M}"
                    " — der gesamte Bestand bleibt unangetastet.")
        rt.store.save(rt.config)
        console.print(f"[green]{employee_id} aktiviert.[/green]")


@users_app.command("disable")
def users_disable(employee_id: int, config: ConfigOpt = None) -> None:
    """Benutzer deaktivieren. Löscht **keine** Termine. Schreibend."""
    with _runtime(config, with_graph=False) as rt, write_lock(rt.config, "users disable"):
        entry = rt.config.user(employee_id)
        if entry is None:
            err.print(f"[red]{employee_id} ist nicht zugeordnet.[/red]")
            raise typer.Exit(2)
        entry.enabled = False
        rt.store.save(rt.config)
        console.print(f"{employee_id} deaktiviert. "
                      "[dim]Seine Termine bleiben in beiden Systemen bestehen.[/dim]")


# ---------------------------------------------------------------------- Token

@token_app.command("status")
def token_status(config: ConfigOpt = None) -> None:
    """Restlaufzeit und nächster Erneuerungstermin. Lesend."""
    from .tanss.auth import next_rotation

    with _runtime(config, with_graph=False) as rt:
        expiry = rt.auth.expires_at()
        remaining = rt.auth.days_remaining()
        console.print(f"[bold]Inhaber[/bold]     Mitarbeiter {rt.auth.owner_employee_id}")
        console.print(f"[bold]Typ[/bold]         {rt.auth.subject}")
        if expiry:
            console.print(f"[bold]Gültig bis[/bold]  {expiry:%d.%m.%Y %H:%M} "
                          f"({remaining} Tage)")
            rotate = next_rotation(expiry, rt.config.tanss.rotate_before_days)
            console.print(f"[bold]Erneuerung[/bold]  ab {rotate:%d.%m.%Y}")
        warning = rt.auth.warning()
        if warning:
            console.print(f"\n[yellow]{warning}[/yellow]")
        console.print("\n[dim]Erneuerung ersetzt das alte Token nicht — es bleibt bis "
                      "zu seinem Ablauf gültig. Einen Widerruf kennt TANSS nicht.[/dim]")


@token_app.command("check-rotation")
def token_check_rotation(config: ConfigOpt = None) -> None:
    """Trockentest der Erneuerung. Erzeugt kein brauchbares Token. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        if rt.auth.can_rotate(rt.tanss.client):
            console.print("[green]Die Erneuerung wird funktionieren.[/green]")
        else:
            err.print("[red]Die Erneuerung würde fehlschlagen.[/red]\n"
                      f"Mitarbeiter {rt.auth.owner_employee_id} braucht das Recht "
                      "„Administration: System API-Tokens für ext. Anbindungen und "
                      "Schnittstellen erzeugen" " und muss aktiv sein.")
            raise typer.Exit(2)


@token_app.command("rotate")
def token_rotate(config: ConfigOpt = None) -> None:
    """Erneuerung sofort auslösen. Schreibend."""
    with _runtime(config, with_graph=False) as rt, write_lock(rt.config, "token rotate"):
        result = rt.auth.rotate_if_needed(
            rt.tanss.client, before_days=10_000,
            verify=lambda candidate: _token_works(rt, candidate))
        if result.rotated:
            console.print(f"[green]Erneuert.[/green] Gültig bis "
                          f"{result.new_expires_at:%d.%m.%Y}")
        else:
            err.print(f"[yellow]Nicht erneuert:[/yellow] {result.reason}")
            if result.error:
                err.print(f"  {result.error}")


# ------------------------------------------------------- Nachvollziehen, Wiederherstellen

def _since_seconds(value: str | None) -> int | None:
    """``7d``, ``48h`` oder ``30`` (Tage) — der Beginn des Zeitraums als Zeitstempel."""
    if not value:
        return None
    raw = str(value).strip().lower()
    faktor = {"d": 86400, "h": 3600, "m": 60}.get(raw[-1:], 86400)
    zahl = raw[:-1] if raw[-1:] in "dhm" else raw
    try:
        return int(datetime.now(UTC).timestamp()) - int(zahl) * faktor
    except ValueError as exc:
        raise typer.BadParameter(f"Zeitraum nicht lesbar: {value}") from exc


@app.command("history")
def history_cmd(
    config: ConfigOpt = None,
    support: Annotated[int | None, typer.Option("--support", help="TANSS-Terminkennung")] = None,
    event: Annotated[str | None, typer.Option("--event", help="Outlook-Terminkennung")] = None,
    user_id: Annotated[int | None, typer.Option("--user", help="Mitarbeiterkennung")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Zeitraum, etwa 7d")] = None,
    limit: int = 50,
) -> None:
    """Was mit einem Termin oder einem Mitarbeiter geschehen ist. Lesend.

    Serien-Occurrences haben in TANSS die Kennung 0 — für sie taugt ``--support``
    nicht; dort hilft ``--user`` zusammen mit einem Zeitraum.
    """
    if not any((support, event, user_id)):
        err.print("[red]Bitte --support, --event oder --user angeben.[/red]")
        raise typer.Exit(2)

    with _runtime(config, with_graph=False) as rt:
        wo, werte = [], []
        if support:
            wo.append("tanss_support_id = ?")
            werte.append(support)
        if event:
            wo.append("graph_event_id = ?")
            werte.append(event)
        if user_id:
            wo.append("employee_id = ?")
            werte.append(user_id)
        ab = _since_seconds(since)
        if ab:
            wo.append("ts >= ?")
            werte.append(ab)

        rows = rt.state.connect().execute(
            "SELECT ts, operation, outcome, side, reason, error FROM audit "
            f"WHERE {' AND '.join(wo)} ORDER BY id DESC LIMIT ?",
            (*werte, limit)).fetchall()

        if not rows:
            console.print("[dim]Kein Eintrag gefunden.[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("Wann")
        table.add_column("Was")
        table.add_column("Seite")
        table.add_column("Ergebnis")
        table.add_column("Begründung")
        for row in reversed(rows):
            wann = datetime.fromtimestamp(row["ts"], UTC).astimezone()
            table.add_row(f"{wann:%d.%m.%Y %H:%M}", row["operation"], row["side"],
                          row["outcome"], row["reason"] or row["error"] or "")
        console.print(table)


@app.command("deleted")
def deleted_cmd(
    config: ConfigOpt = None,
    since: Annotated[str | None, typer.Option("--since", help="Zeitraum, etwa 30d")] = "30d",
    limit: int = 50,
) -> None:
    """Was wurde wann und warum gelöscht. Lesend.

    Vor jeder Löschung wird gesichert. Diese Liste ist der Weg zur Sicherung; mit der
    Kennung daraus stellt ``restore`` den Termin wieder her.
    """
    with _runtime(config, with_graph=False) as rt:
        ab = _since_seconds(since) or 0
        rows = rt.state.connect().execute(
            "SELECT id, deleted_at, side, trigger, mailbox, tanss_support_id, payload "
            "FROM deleted_backup WHERE deleted_at >= ? ORDER BY id DESC LIMIT ?",
            (ab, limit)).fetchall()

        if not rows:
            console.print("[dim]In diesem Zeitraum wurde nichts gelöscht.[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("Sicherung", justify="right")
        table.add_column("Wann")
        table.add_column("Seite")
        table.add_column("Anlass")
        table.add_column("Termin")
        for row in rows:
            wann = datetime.fromtimestamp(row["deleted_at"], UTC).astimezone()
            try:
                betreff = (json.loads(row["payload"]) or {}).get("subject", "")
            except json.JSONDecodeError:
                betreff = ""
            table.add_row(str(row["id"]), f"{wann:%d.%m.%Y %H:%M}", row["side"],
                          row["trigger"], betreff[:44])
        console.print(table)
        console.print("[dim]Wiederherstellen mit: tanss-sync restore <Sicherung>[/dim]")


@app.command("restore")
def restore_cmd(backup_id: int, config: ConfigOpt = None,
                dry_run: DryRun = False) -> None:
    """Einen gelöschten Termin wiederherstellen. Schreibend.

    Wiederhergestellt wird auf der Seite, auf der gelöscht wurde, und ohne Teilnehmer —
    ein Termin mit Teilnehmern löste beim Anlegen Einladungsmails aus, und eine
    Wiederherstellung soll niemandem eine zweite Einladung schicken.
    """
    with _runtime(config) as rt:
        row = rt.state.connect().execute(
            "SELECT * FROM deleted_backup WHERE id = ?", (backup_id,)).fetchone()
        if row is None:
            err.print(f"[red]Keine Sicherung mit der Kennung {backup_id}.[/red]")
            raise typer.Exit(2)

        payload = json.loads(row["payload"])
        wann = datetime.fromtimestamp(row["deleted_at"], UTC).astimezone()
        console.print(f"Sicherung {backup_id}: [bold]{payload.get('subject','')}[/bold]")
        console.print(f"  gelöscht am {wann:%d.%m.%Y %H:%M} auf Seite {row['side']}")
        console.print(f"  Beginn {payload.get('start')} bis {payload.get('end')}")

        if dry_run:
            console.print("[bold]Probelauf[/bold] — nichts wurde angelegt.")
            return

        if row["side"] != "graph":
            err.print("[yellow]Nur in Outlook gelöschte Termine lassen sich hier "
                      "wiederherstellen.[/yellow]")
            err.print("Ein TANSS-Datensatz wird über den Papierkorb in TANSS "
                      "zurückgeholt.")
            raise typer.Exit(2)

        if rt.graph is None:
            err.print("[red]Ohne Microsoft-Zugang ist keine Wiederherstellung "
                      "möglich.[/red]")
            raise typer.Exit(2)

        with write_lock(rt.config, "restore"):
            created = rt.graph.create_event(row["mailbox"], {
                "subject": payload.get("subject") or "Wiederhergestellter Termin",
                "body": {"contentType": "text", "content": payload.get("body") or ""},
                "start": {"dateTime": payload["start"][:19], "timeZone": "UTC"},
                "end": {"dateTime": payload["end"][:19], "timeZone": "UTC"},
                "location": {"displayName": payload.get("location") or ""},
            }, transaction_id=f"restore-{backup_id}")
        console.print(f"[green]Wiederhergestellt[/green] als {created.id}")
        console.print("[dim]Die Kopplung wurde bewusst nicht wiederhergestellt — "
                      "prüfen Sie den Termin, bevor der nächste Abgleich läuft.[/dim]")


@app.command("export-state")
def export_state_cmd(config: ConfigOpt = None) -> None:
    """Alle Verknüpfungen als JSON ausgeben — für Auswertung und Unterstützung. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        rows = rt.state.connect().execute(
            "SELECT * FROM links ORDER BY tanss_employee_id, uid").fetchall()
        console.print_json(json.dumps([dict(r) for r in rows], ensure_ascii=False))


@app.command("unlink")
def unlink_cmd(
    config: ConfigOpt = None,
    user_id: Annotated[int | None, typer.Option("--user", help="Mitarbeiterkennung")] = None,
    support: Annotated[int | None, typer.Option("--support", help="TANSS-Terminkennung")] = None,
    dry_run: DryRun = False,
) -> None:
    """Kopplungen lösen. Beide Seiten bleiben **unverändert**. Schreibend.

    Die Termine bleiben in TANSS und in Outlook vollständig bestehen — sie gehen sich
    ab jetzt nur nichts mehr an. Es gibt keinen Weg, aus einem ``unlink`` eine Löschung
    zu machen.
    """
    if not (user_id or support):
        err.print("[red]Bitte --user oder --support angeben.[/red]")
        raise typer.Exit(2)

    with _runtime(config, with_graph=False) as rt:
        wo = "tanss_employee_id = ?" if user_id else "tanss_support_id = ?"
        wert = user_id if user_id else support
        anzahl = rt.state.connect().execute(
            f"SELECT COUNT(*) AS n FROM links WHERE {wo} AND state = 'linked'",
            (wert,)).fetchone()["n"]

        if not anzahl:
            console.print("[dim]Keine passende Kopplung gefunden.[/dim]")
            return

        console.print(f"{anzahl} Kopplungen würden gelöst.")
        if dry_run:
            console.print("[bold]Probelauf[/bold] — nichts wurde geändert.")
            return

        with write_lock(rt.config, "unlink"):
            rt.state.connect().execute(
                f"UPDATE links SET state = 'detached' WHERE {wo} AND state = 'linked'",
                (wert,))
        console.print(f"[green]{anzahl} Kopplungen gelöst.[/green] "
                      "Die Termine bleiben auf beiden Seiten bestehen.")


# ---------------------------------------------------------------------- Webhooks

def _webhooks(rt):
    from .sync.webhooks import WebhookManager

    return WebhookManager(rt.tanss,
                          rt.config.push.callback_base_url if rt.config.push else "")


@webhooks_app.command("list")
def webhooks_list(config: ConfigOpt = None) -> None:
    """Alle Event-Regeln, mit Kennzeichnung fremder Meldewege. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        report = _webhooks(rt).inspect()

        table = Table(show_header=True, header_style="bold")
        table.add_column("Regel", justify="right")
        table.add_column("Name")
        table.add_column("Mitarbeiter")
        table.add_column("Ziel")
        table.add_column("Herkunft")
        for rule in report.own + report.foreign:
            eigen = rule in report.own
            table.add_row(
                str(rule.id), rule.name or "[dim]ohne Namen[/dim]",
                ", ".join(str(e) for e in rule.employee_ids) or "[dim]alle[/dim]",
                " / ".join(u[:52] for u in rule.webhook_urls),
                "[green]eigen[/green]" if eigen else "[yellow]fremd[/yellow]")
        console.print(table)

        if report.without_webhook:
            console.print(f"[dim]{len(report.without_webhook)} Regeln ohne Webhook — "
                          "sie betreffen den Abgleich nicht.[/dim]")
        if report.duplicates:
            console.print()
            console.print(f"[yellow]{len(report.duplicates)} doppelte Regeln.[/yellow] "
                          "Jede davon feuert einzeln — derselbe Vorgang wird also "
                          "mehrfach gemeldet.")
            console.print("Bereinigen mit: "
                          "[bold]tanss-sync webhooks cleanup --dry-run[/bold]")


@webhooks_app.command("check-tanssx")
def webhooks_check_foreign(config: ConfigOpt = None) -> None:
    """Läuft für einen Mitarbeiter eine andere Terminsynchronisation? Lesend."""
    with _runtime(config, with_graph=False) as rt:
        report = _webhooks(rt).inspect()
        betroffen = report.foreign_employees

        if not betroffen:
            console.print("[green]Keine fremden Meldewege gefunden.[/green]")
            console.print(
                "[dim]Das ist allerdings kein Beweis: Die Richtung Outlook nach TANSS "
                "kommt ohne Event-Regel aus. Eine fremde Synchronisation kann also "
                "laufen, ohne hier aufzutauchen.[/dim]")
            return

        console.print(f"[yellow]Für {len(betroffen)} Mitarbeiter laufen fremde "
                      "Meldewege.[/yellow]")
        for employee_id in sorted(betroffen):
            entry = rt.config.user(employee_id)
            name = entry.tanss_email if entry else "nicht zugeordnet"
            aktiv = " [red]— bei uns aktiviert[/red]" if entry and entry.enabled else ""
            console.print(f"  {employee_id:>5}  {name}{aktiv}")
        console.print()
        console.print("Zwei Systeme, die dieselben Kalender abgleichen, schreiben "
                      "gegeneinander und erzeugen Duplikate.")
        raise typer.Exit(1)


@webhooks_app.command("cleanup")
def webhooks_cleanup(
    config: ConfigOpt = None,
    dry_run: DryRun = False,
    include_foreign: Annotated[bool, typer.Option(
        "--include-foreign",
        help="Auch überzählige Regeln einer fremden Terminsynchronisation entfernen"
    )] = False,
) -> None:
    """Doppelte Regeln entfernen. Je Mitarbeiter und Dienst bleibt die älteste. Schreibend.

    Melden mehrere Regeln denselben Mitarbeiter an denselben Dienst, feuert jede
    einzeln — derselbe Vorgang wird dann mehrfach gemeldet.

    Fremde Regeln werden **nur mit** ``--include-foreign`` angefasst. Sie gehören einem
    anderen System; sie zu entfernen greift in dessen Betrieb ein. Die jeweils älteste
    bleibt immer stehen, das fremde System arbeitet also weiter — nur eben einmal statt
    dreimal je Vorgang.
    """
    with _runtime(config, with_graph=False) as rt:
        manager = _webhooks(rt)
        report = manager.inspect()

        if not report.duplicates:
            console.print("[green]Keine doppelten Regeln.[/green]")
            return

        zu_entfernen = []
        for group in report.groups:
            bleibt = min(r.id for r in group.rules)
            herkunft = "[yellow]fremd[/yellow]" if group.is_foreign else "eigen"
            console.print(f"Mitarbeiter {group.employee_id} nach {group.url[:46]} "
                          f"({herkunft})")
            console.print(f"  behalten: {bleibt}   überzählig: "
                          + ", ".join(str(r.id) for r in group.duplicates))
            if group.is_foreign and not include_foreign:
                console.print("  [dim]übersprungen — gehört einer fremden "
                              "Terminsynchronisation[/dim]")
                continue
            zu_entfernen.extend(group.duplicates)

        console.print()
        if not zu_entfernen:
            console.print("[yellow]Alle Duplikate gehören einer fremden "
                          "Terminsynchronisation.[/yellow]")
            console.print("Mitentfernen mit: [bold]tanss-sync webhooks cleanup "
                          "--include-foreign --dry-run[/bold]")
            return

        if dry_run:
            console.print(f"[bold]Probelauf[/bold] — {len(zu_entfernen)} Regeln würden "
                          "entfernt, nichts wurde geändert.")
            return

        with write_lock(rt.config, "webhooks cleanup"):
            entfernt = 0
            for rule in zu_entfernen:
                try:
                    manager.remove(rule)
                    entfernt += 1
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Regel {rule.id} nicht entfernt:[/red] {exc}")
        console.print(f"[green]{entfernt} überzählige Regeln entfernt.[/green]")


@webhooks_app.command("sync")
def webhooks_sync(config: ConfigOpt = None, dry_run: DryRun = False) -> None:
    """Eigene Regeln abgleichen — je aktiviertem Mitarbeiter eine. Schreibend.

    Nur im Push-Betrieb sinnvoll: Im Poll-Betrieb fragt der Dienst selbst nach und
    braucht keinen Meldeweg.
    """
    with _runtime(config, with_graph=False) as rt:
        if rt.config.push is None:
            err.print("[red]Ohne push-Abschnitt gibt es keine Rückrufadresse.[/red]")
            err.print("Eigene Regeln sind nur bei sync.mode push nötig.")
            raise typer.Exit(2)

        manager = _webhooks(rt)
        report = manager.inspect()
        aktiv = [u.tanss_employee_id for u in rt.config.users if u.enabled]
        fehlend = manager.missing_for(aktiv, report)

        if not fehlend:
            console.print(f"[green]Alle {len(aktiv)} aktivierten Mitarbeiter haben "
                          "eine eigene Regel.[/green]")
            return

        console.print("Fehlende Regeln: " + ", ".join(str(e) for e in fehlend))
        if dry_run:
            console.print("[bold]Probelauf[/bold] — nichts wurde angelegt.")
            return

        with write_lock(rt.config, "webhooks sync"):
            for employee_id in fehlend:
                try:
                    rule_id = manager.create_for(employee_id)
                    console.print(f"  [green]+[/green] Regel {rule_id} für {employee_id}")
                except Exception as exc:  # noqa: BLE001
                    err.print(f"  [red]Regel für {employee_id} nicht angelegt:[/red] {exc}")


# ---------------------------------------------------------------------- Datenbank

@db_app.command("check")
def db_check(config: ConfigOpt = None) -> None:
    """Integrität der Zustandsdatenbank prüfen. Lesend."""
    with _runtime(config, with_graph=False) as rt:
        ok = rt.state.integrity_check()
        stats = rt.state.stats()
        console.print(("[green]in Ordnung[/green]" if ok else "[red]beschädigt[/red]")
                      + f" — {rt.config.state.db_path}")
        for table, count in stats.items():
            console.print(f"  {table:<18} {count:>8}")
        raise typer.Exit(0 if ok else 2)


@db_app.command("backup")
def db_backup(target: str, config: ConfigOpt = None) -> None:
    """Konsistente Sicherung ziehen. Lesend.

    Verknüpfungen lassen sich aus beiden Systemen rekonstruieren — das
    Änderungsprotokoll und die Sicherungen gelöschter Termine **nicht**.
    """
    with _runtime(config, with_graph=False) as rt:
        rt.state.backup_to(target)
        console.print(f"Gesichert nach {target}")


@db_app.command("vacuum")
def db_vacuum(config: ConfigOpt = None) -> None:
    """Datenbank verdichten. Schreibend."""
    with _runtime(config, with_graph=False) as rt, write_lock(rt.config, "db vacuum"):
        rt.state.vacuum()
        console.print("Verdichtet.")


# ---------------------------------------------------------------------- intern

class _FixedToken:
    """Hilfsklasse: prüft ein Token, ohne es zu übernehmen."""

    def __init__(self, token: str) -> None:
        self._token = token

    def header(self) -> dict[str, str]:
        value = self._token
        if not value.startswith("Bearer "):
            value = f"Bearer {value}"
        return {"apiToken": value}


def _foreign_sync_employees(rt) -> set[int]:
    """Mitarbeiter mit einer fremden Terminsynchronisation.

    Eine Regel mit fremder Ziel-Adresse ist ein **harter Beleg** dafür, dass ein anderes
    System denselben Kalender abgleicht. Das Fehlen einer Regel ist umgekehrt **kein**
    Beleg für das Gegenteil — die Gegenrichtung läuft ohne Event-Regel.
    """
    own = (rt.config.push.callback_base_url if rt.config.push else "")
    found: set[int] = set()
    try:
        for rule in rt.tanss.list_event_rules():
            urls = rule.webhook_urls
            if not urls:
                continue
            if own and all(own in url for url in urls):
                continue
            found.update(rule.employee_ids)
    except Exception:  # noqa: BLE001 - Diagnose darf den Befehl nicht kippen
        return set()
    return found


def _runtime(config: str | None, *, with_graph: bool = True):
    try:
        return open_runtime(config, with_graph=with_graph)
    except ConfigError as exc:
        _offer_setup(exc)
        raise typer.Exit(1) from exc


def _offer_setup(exc: ConfigError) -> None:
    err.print(f"[red]{exc}[/red]")
    store = ConfigStore.discover()
    if not store.exists():
        err.print("\nNoch keine Konfiguration vorhanden.")
        err.print("Einrichtung starten mit: [bold]tanss-sync setup[/bold]")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
