"""An- und Abfahrt gehen im selben Lauf wie ihr Haupttermin.

Zuerst behoben war nur, *dass* sie überhaupt mitgehen — aber eine Runde später, weil
die Fahrt erst aufgriffen wurde, nachdem die Kopplung des Haupttermins bereits auf
``deleted`` stand. Dazwischen standen Anfahrt und Abfahrt ohne ihren Termin im
Kalender. Wer in dem Moment hinsieht, hält den Abgleich für kaputt.

Der Nachweis des Haupttermins (``tanss_404``) trägt seine Fahrten mit: Sie sind seine
Projektion und haben keine eigene Vorlage in TANSS.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.identity import SyncDirection, SyncKey
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.engine import fahrten_nach_haupttermin
from tanss_sync.sync.reconciler import Pair

MB = "s.michel@pronet-systems.de"
UID = "040000008200E00074C5B7101A82E008000000009B1FEC61E641DD01"


def _paar(rolle: str, *, state: str = "linked", in_outlook: bool = True) -> Pair:
    key = SyncKey(MB, UID, -1, rolle)
    art = {"main": AppointmentKind.FIXED, "travel_to": AppointmentKind.TRAVEL_TO,
           "travel_back": AppointmentKind.TRAVEL_BACK}[rolle]
    return Pair(
        tanss=None,
        graph=Appointment(key=key, kind=art, subject=rolle,
                          start=datetime(2026, 9, 11, 9, 30, tzinfo=UTC),
                          end=datetime(2026, 9, 11, 10, 0, tzinfo=UTC),
                          graph_event_id=f"ev-{rolle}") if in_outlook else None,
        link=LinkRecord(mailbox=MB, uid=UID, sequence=-1, travel_role=rolle,
                        tanss_support_id=59380, graph_event_id=f"ev-{rolle}",
                        tanss_employee_id=1, state=state,
                        write_direction=SyncDirection.TANSS_TO_M365
                        if rolle != "main" else SyncDirection.BOTH),
    )


def test_beide_fahrten_haengen_am_haupttermin() -> None:
    gruppen = fahrten_nach_haupttermin(
        [_paar("main"), _paar("travel_to"), _paar("travel_back")])
    assert sorted(a.key.travel_role for a in gruppen[UID]) == ["travel_back", "travel_to"]


def test_haupttermin_selbst_zaehlt_nicht_als_fahrt() -> None:
    assert fahrten_nach_haupttermin([_paar("main")]) == {}


def test_was_in_outlook_fehlt_ist_nicht_zu_loeschen() -> None:
    assert fahrten_nach_haupttermin([_paar("travel_to", in_outlook=False)]) == {}


def test_geloeste_kopplung_geht_den_haupttermin_nichts_mehr_an() -> None:
    assert fahrten_nach_haupttermin([_paar("travel_to", state="detached")]) == {}
