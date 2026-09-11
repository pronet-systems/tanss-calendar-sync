"""Übernahme statt Verdopplung.

Diese Regeln sind die Antwort auf einen echten Vorfall: Ein Lauf gegen den Bestand legte
jede Abwesenheit ein zweites Mal an, weil das zuvor eingesetzte Produkt zwar nach Outlook
geschrieben, in TANSS aber keine Kopplungsdaten hinterlassen hat.

Die Fälle unten sind den Daten des Kundensystems nachgebildet, einschließlich der
beiden Eigenheiten, die dort tatsächlich vorkommen: Ein Teil der Bestandstermine trägt
das ``(Firma: …)``-Suffix, ein Teil nicht — und die Tagesspanne einer Abwesenheit ist
nicht dieselbe wie in TANSS.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.identity import SyncKey
from tanss_sync.sync.adoption import find_adoption
from tanss_sync.util.timezone import TimeConverter

MAILBOX = "mitarbeiter@example.com"
LOCAL = TimeConverter("W. Europe Standard Time").local


def moment(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


def appointment(subject: str, start: dt.datetime, minutes: int, *,
                kind: AppointmentKind = AppointmentKind.FIXED,
                event_id: str | None = None) -> Appointment:
    made = Appointment(key=SyncKey(MAILBOX, f"{subject}{start.timestamp():.0f}", 0, "main"), kind=kind)
    made.subject = subject
    made.start = start
    made.end = start + dt.timedelta(minutes=minutes)
    made.graph_event_id = event_id
    return made


def vacation(start: dt.datetime, minutes: int = 540) -> Appointment:
    return appointment("Urlaub (Firma: ProNet Systems GmbH)", start, minutes,
                       kind=AppointmentKind.VACATION)


def find(source: Appointment, *candidates: Appointment):
    return find_adoption(source, list(candidates), to_local=LOCAL)


# --------------------------------------------------------------------- übernehmen

def test_gleiche_zeit_und_betreff_wird_uebernommen() -> None:
    source = vacation(moment(2026, 3, 2, 7))
    existing = appointment("Urlaub (Firma: ProNet Systems GmbH)", moment(2026, 3, 2, 7),
                           540, event_id="EV1")
    assert find(source, existing).candidate is existing


def test_firmensuffix_entscheidet_nicht() -> None:
    """Im Bestand tragen es manche Termine und manche nicht."""
    source = vacation(moment(2026, 3, 2, 7))
    existing = appointment("Urlaub", moment(2026, 3, 2, 7), 540, event_id="EV2")
    assert find(source, existing).candidate is existing


def test_abwesenheit_mit_abweichender_tagesspanne() -> None:
    """Ein Urlaubstag ist ein Tagesfakt — welche Stunden ein System wählt, ist Konvention."""
    source = vacation(moment(2026, 3, 2, 7))
    ganztags = appointment("Urlaub", moment(2026, 3, 1, 23), 24 * 60, event_id="EV3")
    found = find(source, ganztags)
    assert found is not None and found.candidate is ganztags


def test_termin_mit_gleicher_zeit_wird_uebernommen() -> None:
    source = appointment("Jour fixe", moment(2026, 3, 2, 9), 60)
    existing = appointment("Jour fixe", moment(2026, 3, 2, 9), 60, event_id="EV4")
    assert find(source, existing).candidate is existing


# ------------------------------------------------------------------ nicht übernehmen

def test_termin_mit_abweichender_zeit_wird_nicht_uebernommen() -> None:
    """Für einen Termin gilt die Minute — die Tagesregel greift nur bei Abwesenheiten."""
    source = appointment("Jour fixe", moment(2026, 3, 2, 9), 60)
    spaeter = appointment("Jour fixe", moment(2026, 3, 2, 10), 60, event_id="EV5")
    assert find(source, spaeter) is None


def test_mehrdeutigkeit_bricht_ab() -> None:
    """Lieber ein sichtbares Duplikat als eine unsichtbare Fehlkopplung."""
    source = vacation(moment(2026, 3, 2, 7))
    vormittags = appointment("Urlaub", moment(2026, 3, 2, 7), 240, event_id="EV6")
    nachmittags = appointment("Urlaub", moment(2026, 3, 2, 12), 300, event_id="EV7")
    assert find(source, vormittags, nachmittags) is None


def test_anderer_tag_wird_nicht_uebernommen() -> None:
    source = vacation(moment(2026, 3, 2, 7))
    tags_darauf = appointment("Urlaub", moment(2026, 3, 3, 7), 540, event_id="EV8")
    assert find(source, tags_darauf) is None


def test_anderer_betreff_wird_nicht_uebernommen() -> None:
    source = vacation(moment(2026, 3, 2, 7))
    krank = appointment("Krankheit", moment(2026, 3, 2, 7), 540, event_id="EV9")
    assert find(source, krank) is None


def test_kandidat_ohne_termin_kennung_scheidet_aus() -> None:
    source = vacation(moment(2026, 3, 2, 7))
    ohne_id = appointment("Urlaub", moment(2026, 3, 2, 7), 540)
    assert find(source, ohne_id) is None


@pytest.mark.parametrize("subject", ["", "   "])
def test_leerer_betreff_wird_nie_uebernommen(subject: str) -> None:
    """Ohne Betreff bliebe als Merkmal nur die Uhrzeit — das genügt nicht."""
    source = appointment(subject, moment(2026, 3, 2, 7), 540)
    existing = appointment(subject, moment(2026, 3, 2, 7), 540, event_id="EV10")
    assert find(source, existing) is None
