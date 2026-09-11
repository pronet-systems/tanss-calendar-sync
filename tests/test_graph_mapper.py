"""Wie ein Outlook-Termin gelesen wird.

Der Kern ist eine Unterscheidung, die Graph selbst nicht macht: ``responseStatus`` meldet
``none`` sowohl bei einer **offenen Einladung** als auch bei einem Termin, den jemand
sich selbst eingetragen hat. Beides gleich zu behandeln, macht aus jedem eigenen Termin
eine Terminvormerkung in TANSS — orange statt fest, und im Status „angefragt", obwohl
nie jemand gefragt wurde.

Am Kundensystem aufgefallen: Ein Testtermin ohne Teilnehmer wäre als
``APPOINTMENT_PROPOSAL`` in TANSS gelandet.
"""

from __future__ import annotations

import pytest

from tanss_sync.domain.appointment import AppointmentKind
from tanss_sync.microsoft.mapper import GraphMapper
from tanss_sync.microsoft.models import GraphEvent
from tanss_sync.tanss.mapper import STATUS_FROM_GRAPH
from tanss_sync.util.timezone import TimeConverter

MAILBOX = "s.michel@example.com"


def event(*, attendees: list | None = None, response: str = "none",
          sensitivity: str = "normal") -> GraphEvent:
    return GraphEvent.model_validate({
        "id": "EV1",
        "iCalUId": "uid-1",
        "subject": "Test",
        "start": {"dateTime": "2026-09-11T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-11T09:30:00.0000000", "timeZone": "UTC"},
        "attendees": attendees or [],
        "responseStatus": {"response": response},
        "sensitivity": sensitivity,
        "organizer": {"emailAddress": {"address": MAILBOX, "name": "Sebastian Michel"}},
    })


def gast(address: str = "kunde@example.org", response: str = "none") -> dict:
    return {"emailAddress": {"address": address, "name": address},
            "type": "required", "status": {"response": response}}


def mapped(ev: GraphEvent):
    return GraphMapper(TimeConverter()).to_appointment(ev, MAILBOX, "uid-1")


# ------------------------------------------------------- selbst eingetragener Termin

def test_termin_ohne_eingeladene_ist_fest() -> None:
    """Niemand hat eine Einladung offen gelassen — es ist schlicht ein eigener Termin."""
    assert mapped(event()).kind is AppointmentKind.FIXED


def test_termin_ohne_eingeladene_gilt_als_zugesagt() -> None:
    ergebnis = mapped(event())
    assert ergebnis.own_response == "organizer"
    assert STATUS_FROM_GRAPH[ergebnis.own_response] == "ACCEPTED"


@pytest.mark.parametrize("response", ["none", "notResponded"])
def test_auch_ohne_antwortstatus_bleibt_er_fest(response: str) -> None:
    assert mapped(event(response=response)).kind is AppointmentKind.FIXED


# ------------------------------------------------------------------ echte Einladung

@pytest.mark.parametrize("response", ["none", "notResponded"])
def test_offene_einladung_ist_eine_vormerkung(response: str) -> None:
    """Hier gab es tatsächlich etwas zuzusagen."""
    ergebnis = mapped(event(attendees=[gast()], response=response))
    assert ergebnis.kind is AppointmentKind.TENTATIVE
    assert STATUS_FROM_GRAPH[ergebnis.own_response] == "REQUESTED"


def test_angenommene_einladung_ist_fest() -> None:
    ergebnis = mapped(event(attendees=[gast()], response="accepted"))
    assert ergebnis.kind is AppointmentKind.FIXED
    assert STATUS_FROM_GRAPH[ergebnis.own_response] == "ACCEPTED"


def test_unter_vorbehalt_zaehlt_als_zusage() -> None:
    """TANSS kennt kein „unter Vorbehalt" — es zählt als volle Zusage."""
    ergebnis = mapped(event(attendees=[gast()], response="tentativelyAccepted"))
    assert STATUS_FROM_GRAPH[ergebnis.own_response] == "ACCEPTED"


def test_teilnehmer_ohne_adresse_zaehlt_nicht_als_eingeladener() -> None:
    leer = {"emailAddress": {}, "type": "required"}
    assert mapped(event(attendees=[leer])).kind is AppointmentKind.FIXED


# ------------------------------------------------------------------ privat

def test_privater_termin_bleibt_privat() -> None:
    """Die Vertraulichkeit schlägt die Antwortlage."""
    ergebnis = mapped(event(attendees=[gast()], sensitivity="private"))
    assert ergebnis.kind is AppointmentKind.PRIVATE
