"""Ein- und Ausgabe des Einrichtungsassistenten.

Bewusst von den Schritten getrennt: Die Schritte enthalten damit ausschließlich
Fachlogik und lassen sich ohne Terminal prüfen — im Test tritt eine Fassung an diese
Stelle, die vorbereitete Antworten liefert.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()


class Asker:
    """Fragt im Terminal."""

    def heading(self, text: str) -> None:
        console.print()
        console.rule(f"[bold]{text}[/bold]")

    def info(self, text: str) -> None:
        console.print(text)

    def success(self, text: str) -> None:
        console.print(f"[green]✓[/green] {text}")

    def failure(self, message: str, hint: str = "") -> None:
        console.print(f"[red]✗ {message}[/red]")
        if hint:
            console.print(f"  [yellow]{hint}[/yellow]")

    def text(self, question: str, default: str | None = None) -> str:
        return Prompt.ask(question, default=default or None) or ""

    def secret(self, question: str) -> str:
        return Prompt.ask(question, password=True, default="") or ""

    def number(self, question: str, default: int | None = None) -> int:
        while True:
            raw = Prompt.ask(question,
                             default=str(default) if default is not None else None)
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                console.print("[red]Bitte eine ganze Zahl eingeben.[/red]")

    def choice(self, question: str, options: list[str], default: str | None = None) -> str:
        return Prompt.ask(question, choices=options, default=default or options[0])

    def confirm(self, question: str, default: bool = True) -> bool:
        return Confirm.ask(question, default=default)


class ScriptedAsker:
    """Antwortet aus einer vorbereiteten Liste — für Tests und den nicht-interaktiven Lauf.

    Fehlt eine Antwort, wird der Vorgabewert genommen. So lässt sich ein Durchlauf
    prüfen, ohne jede Frage zu bedienen.
    """

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = answers or {}
        self.log: list[str] = []

    def heading(self, text: str) -> None:
        self.log.append(f"# {text}")

    def info(self, text: str) -> None:
        self.log.append(text)

    def success(self, text: str) -> None:
        self.log.append(f"ok: {text}")

    def failure(self, message: str, hint: str = "") -> None:
        self.log.append(f"fehler: {message} | {hint}")

    def _answer(self, question: str, default):
        return self.answers.get(question, default)

    def text(self, question: str, default: str | None = None) -> str:
        return str(self._answer(question, default) or "")

    def secret(self, question: str) -> str:
        return str(self._answer(question, "") or "")

    def number(self, question: str, default: int | None = None) -> int:
        return int(self._answer(question, default) or 0)

    def choice(self, question: str, options: list[str], default: str | None = None) -> str:
        return str(self._answer(question, default or options[0]))

    def confirm(self, question: str, default: bool = True) -> bool:
        return bool(self._answer(question, default))
