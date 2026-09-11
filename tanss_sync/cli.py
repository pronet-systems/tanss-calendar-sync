"""Kommandozeile.

Jedes Kommando ist entweder **lesend** oder **schreibend**. Schreibende nehmen die
Prozess-Sperre und brechen ab, wenn ein anderer Lauf sie hält.
"""

from __future__ import annotations

import sys
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
app.add_typer(users_app, name="users")
app.add_typer(token_app, name="token")
app.add_typer(db_app, name="db")

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
            _print_actions(rt.state)
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


def _print_actions(state, limit: int = 60) -> None:
    """Zeigt, was der letzte Lauf getan hätte oder getan hat."""
    rows = state.connect().execute(
        "SELECT operation, outcome, reason, mailbox, uid, changed_fields "
        "FROM audit WHERE run_id = (SELECT MAX(run_id) FROM audit) "
        "AND side != 'system' ORDER BY id LIMIT ?", (limit,)).fetchall()
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
        def verify(candidate: str) -> bool:
            from .tanss.client import TANSS_X_PREFIX, TanssClient

            probe = TanssClient(rt.config.tanss.api_base,
                                _FixedToken(candidate),
                                timeout=rt.config.tanss.timeout_seconds,
                                verify_tls=rt.config.tanss.verify_tls)
            try:
                probe.get(f"{TANSS_X_PREFIX}/technicians")
                return True
            except Exception:  # noqa: BLE001
                return False
            finally:
                probe.close()

        result = rt.auth.rotate_if_needed(rt.tanss.client, before_days=10_000,
                                          verify=verify)
        if result.rotated:
            console.print(f"[green]Erneuert.[/green] Gültig bis "
                          f"{result.new_expires_at:%d.%m.%Y}")
        else:
            err.print(f"[yellow]Nicht erneuert:[/yellow] {result.reason}")
            if result.error:
                err.print(f"  {result.error}")


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
