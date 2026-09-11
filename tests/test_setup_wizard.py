"""Der Einrichtungsassistent.

Geprüft wird vor allem der Ablauf: Was passiert bei einem Fehlschlag, was bei einer
Warnung, und überlebt ein Abbruch die bereits erledigte Arbeit. Die Schritte selbst
sprechen mit echten Gegenstellen und werden hier durch einfache Attrappen ersetzt.
"""

from __future__ import annotations

from tanss_sync.config.store import ConfigStore
from tanss_sync.setup.prompts import ScriptedAsker
from tanss_sync.setup.wizard import SetupContext, SetupStep, SetupWizard, StepResult


class Zaehlschritt(SetupStep):
    """Ein Schritt, der mitzählt, wie oft er befragt wurde."""

    title = "Test"

    def __init__(self, results: list[StepResult]) -> None:
        self.results = list(results)
        self.prompts = 0

    def prompt(self, ctx: SetupContext, ask) -> None:
        self.prompts += 1
        ctx.section("tanss")["base_url"] = ask.text("Adresse", default="https://x/backend")

    def verify(self, ctx: SetupContext) -> StepResult:
        return self.results.pop(0) if self.results else StepResult(True)


def wizard(tmp_path, steps: list[SetupStep], answers: dict | None = None):
    from tanss_sync.setup import wizard as modul

    store = ConfigStore(tmp_path / "config.json")
    ask = ScriptedAsker(answers)
    original = modul.STEPS
    modul.STEPS = [lambda s=s: s for s in steps]  # Klassen werden aufgerufen
    try:
        return SetupWizard(store, ask), store, ask
    finally:
        modul.STEPS = original


def run(tmp_path, steps: list[SetupStep], answers: dict | None = None):
    from tanss_sync.setup import wizard as modul

    store = ConfigStore(tmp_path / "config.json")
    ask = ScriptedAsker(answers)
    original = modul.STEPS
    modul.STEPS = [lambda s=s: s for s in steps]
    try:
        result = SetupWizard(store, ask).run()
    finally:
        modul.STEPS = original
    return result, store, ask


# ------------------------------------------------------------------ Fehlschlag

def test_fehlgeschlagener_schritt_wird_wiederholt(tmp_path) -> None:
    schritt = Zaehlschritt([StepResult(False, "geht nicht"), StepResult(True, "jetzt ja")])
    run(tmp_path, [schritt], {"Noch einmal versuchen?": True})
    assert schritt.prompts == 2


def test_abbruch_beendet_den_assistenten(tmp_path) -> None:
    schritt = Zaehlschritt([StepResult(False, "geht nicht")])
    ergebnis, _, _ = run(tmp_path, [schritt], {"Noch einmal versuchen?": False})
    assert ergebnis is None


def test_abbruch_bewahrt_den_stand(tmp_path) -> None:
    """Ein geschlossenes Fenster darf keine erledigte Arbeit kosten."""
    schritt = Zaehlschritt([StepResult(False, "geht nicht")])
    _, store, _ = run(tmp_path, [schritt], {"Noch einmal versuchen?": False})
    stand = store.load_partial()
    assert stand is not None and stand["tanss"]["base_url"] == "https://x/backend"


# ------------------------------------------------------------------ Warnung

def test_warnung_wird_nicht_wiederholt(tmp_path) -> None:
    """Der Warnschritt fragt nichts ab — ein Wiederholen liefe endlos."""
    schritt = Zaehlschritt([StepResult(False, "läuft parallel", advisory=True)])
    run(tmp_path, [schritt], {"Trotzdem fortfahren?": True})
    assert schritt.prompts == 1


def test_warnung_laesst_sich_zum_abbruch_nutzen(tmp_path) -> None:
    schritt = Zaehlschritt([StepResult(False, "läuft parallel", advisory=True)])
    ergebnis, _, _ = run(tmp_path, [schritt], {"Trotzdem fortfahren?": False})
    assert ergebnis is None


# ------------------------------------------------------------------ Fortsetzen

def test_angefangene_einrichtung_wird_fortgesetzt(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save_partial({"tanss": {"base_url": "https://vorher/backend"}})

    from tanss_sync.setup import wizard as modul

    gesehen = {}

    class Merker(SetupStep):
        title = "Merker"

        def prompt(self, ctx, ask) -> None:
            gesehen["url"] = ctx.section("tanss").get("base_url")

        def verify(self, ctx) -> StepResult:
            return StepResult(True)

    original = modul.STEPS
    modul.STEPS = [Merker]
    try:
        SetupWizard(store, ScriptedAsker()).run()
    finally:
        modul.STEPS = original

    assert gesehen["url"] == "https://vorher/backend"


# ------------------------------------------------------------------ Antworten

def test_vorbereitete_antworten_gewinnen_ueber_vorgaben() -> None:
    ask = ScriptedAsker({"Adresse": "https://echt/backend"})
    assert ask.text("Adresse", default="https://vorgabe") == "https://echt/backend"


def test_ohne_antwort_gilt_die_vorgabe() -> None:
    assert ScriptedAsker().number("Tage", default=90) == 90
