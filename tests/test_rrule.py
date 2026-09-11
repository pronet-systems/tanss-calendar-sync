"""Wiederholungsregeln in beide Richtungen.

Die Prüfsteine sind die drei Stellen, an denen eine Serie nach der Übertragung auf
anderen Tagen liegen würde als vorher: endlose Serien, die Zeitzone des Abschlusses und
ein Wochentag, der in der Regel nicht ausgeschrieben steht.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tanss_sync.util.rrule import (
    UnsupportedRecurrence,
    endless_cutoff,
    from_graph,
    parse_rrule,
    to_graph,
)

BERLIN = "Europe/Berlin"
# Die Regel eines echten Serientermins aus dem Kundensystem.
ECHT = "FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261027T141500Z"
START = dt.datetime(2026, 5, 19, 14, 0, tzinfo=dt.UTC)  # 16:00 Berliner Zeit


# --------------------------------------------------------------------------- lesen

def test_echte_regel_wird_zerlegt() -> None:
    rule = parse_rrule(ECHT)
    assert rule.freq == "WEEKLY"
    assert rule.by_day == ("TU", "TH")
    assert rule.until == dt.datetime(2026, 10, 27, 14, 15, tzinfo=dt.UTC)
    assert rule.interval == 1


def test_praefix_wird_vertragen() -> None:
    assert parse_rrule("RRULE:FREQ=DAILY").freq == "DAILY"


@pytest.mark.parametrize("value", ["", "FREQ=HOURLY", "FREQ=WEEKLY;BYDAY=XX"])
def test_unlesbare_regel_ist_ein_fehler(value: str) -> None:
    """Kein stiller Rückfall — eine halb übertragene Serie fällt erst auf,
    wenn jemand zum falschen Termin erscheint."""
    with pytest.raises(UnsupportedRecurrence):
        parse_rrule(value)


# ------------------------------------------------------------------- nach Graph

def test_echte_regel_nach_graph() -> None:
    graph = to_graph(ECHT, START, BERLIN)
    assert graph["pattern"] == {"type": "weekly", "interval": 1,
                                "daysOfWeek": ["tuesday", "thursday"]}
    assert graph["range"]["type"] == "endDate"
    assert graph["range"]["recurrenceTimeZone"] == BERLIN
    assert graph["range"]["startDate"] == "2026-05-19"


def test_wochentag_kommt_aus_dem_start_wenn_die_regel_ihn_nicht_nennt() -> None:
    """Graph verlangt ``daysOfWeek`` ausdrücklich; RFC 5545 leitet ihn vom Start ab."""
    graph = to_graph("FREQ=WEEKLY", START, BERLIN)
    assert graph["pattern"]["daysOfWeek"] == ["tuesday"]  # 19.05.2026 ist ein Dienstag


def test_abschluss_wird_in_ortszeit_gelesen() -> None:
    """22:30 UTC ist in Berlin bereits der Folgetag — sonst fiele der letzte Termin weg."""
    graph = to_graph("FREQ=DAILY;UNTIL=20260630T223000Z", START, BERLIN)
    assert graph["range"]["endDate"] == "2026-07-01"


def test_anzahl_statt_enddatum() -> None:
    graph = to_graph("FREQ=WEEKLY;COUNT=10", START, BERLIN)
    assert graph["range"]["type"] == "numbered"
    assert graph["range"]["numberOfOccurrences"] == 10


def test_monatlich_nach_wochentag() -> None:
    graph = to_graph("FREQ=MONTHLY;BYDAY=TU;INTERVAL=2", START, BERLIN)
    assert graph["pattern"]["type"] == "relativeMonthly"
    assert graph["pattern"]["interval"] == 2


def test_monatlich_nach_datum_nimmt_den_starttag() -> None:
    graph = to_graph("FREQ=MONTHLY", START, BERLIN)
    assert graph["pattern"]["dayOfMonth"] == 19


# ------------------------------------------------------------------- nach TANSS

def test_endlose_serie_braucht_ein_enddatum() -> None:
    """TANSS lehnt endlose Serien ab. Eine geratene Spanne wäre eine Behauptung."""
    endlos = {"pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"]},
              "range": {"type": "noEnd", "startDate": "2026-05-19"}}
    with pytest.raises(UnsupportedRecurrence):
        from_graph(endlos, BERLIN)

    regel = from_graph(endlos, BERLIN, endless_end=endless_cutoff(START, 2))
    assert regel.startswith("FREQ=WEEKLY;BYDAY=MO;UNTIL=2028")


def test_graph_enddatum_behaelt_den_letzten_termin() -> None:
    """Bis zum Ende des Tages in Ortszeit — nicht bis Mitternacht UTC."""
    serie = {"pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["tuesday"]},
             "range": {"type": "endDate", "startDate": "2026-05-19",
                       "endDate": "2026-10-27", "recurrenceTimeZone": BERLIN}}
    regel = from_graph(serie, BERLIN)
    assert "UNTIL=20261027T225959Z" in regel


def test_ruecklauf_erhaelt_die_aussage_der_regel() -> None:
    """Hin und zurück darf Frequenz, Abstand und Wochentage nicht verändern."""
    graph = to_graph(ECHT, START, BERLIN)
    zurueck = parse_rrule(from_graph(graph, BERLIN))
    original = parse_rrule(ECHT)
    assert zurueck.freq == original.freq
    assert zurueck.by_day == original.by_day
    assert zurueck.interval == original.interval
    # Der Abschluss wandert auf das Tagesende - derselbe Kalendertag, spaetere Uhrzeit.
    assert zurueck.until is not None and original.until is not None
    assert zurueck.until.date() == original.until.date()
    assert zurueck.until >= original.until


def test_unbekanntes_graph_muster_ist_ein_fehler() -> None:
    with pytest.raises(UnsupportedRecurrence):
        from_graph({"pattern": {"type": "stuendlich"}, "range": {}}, BERLIN)
