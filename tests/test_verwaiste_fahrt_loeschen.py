"""Wird der Haupttermin in TANSS gelöscht, gehen seine Fahrtblöcke mit.

Am Produktivsystem: Ein Termin wurde in TANSS gelöscht. Der Dienst entfernte
daraufhin den Outlook-Termin — Anfahrt und Abfahrt blieben stehen. Im Kalender
standen zwei Fahrtblöcke und dazwischen nichts.

``deletion_candidates`` verlangte bis dahin, dass der Haupttermin **im selben Lauf
vorlag**, bevor eine Fahrtzeile überhaupt als Löschkandidat gelten durfte. Der Schutz ist
berechtigt und bleibt: Ein Termin von vor dem Aktivierungsstichtag fehlt in der
TANSS-Liste, existiert aber weiter — die Einzelabfrage fände ihn lebendig vor und schlösse
daraus fälschlich, die Fahrtzeit sei entfernt worden. Jeder ältere Termin verlöre so seine
Fahrt-Blöcke.

Was fehlte, war der Gegenbeleg. „Gelöscht" und „nur nicht in dieser Runde" unterscheidet
der Zustand der Kopplung des Haupttermins: ``deleted`` entsteht ausschließlich nach einer
Löschung mit Nachweis am Einzelobjekt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tanss_sync.config.models import SyncConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.identity import SyncDirection, SyncKey, UserMapping
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.echo import EchoGuard
from tanss_sync.sync.reconciler import Pair, Reconciler
from tanss_sync.sync.rules import SyncRules

POSTFACH = "s.michel@pronet-systems.de"
HAUPT_UID = "040000008200E00074C5B7101A82E008000000009B1FEC61E641DD01"
SUPPORT = 59380


def _user() -> UserMapping:
    return UserMapping(tanss_employee_id=1, tanss_name="Michel, Sebastian",
                       tanss_email=POSTFACH, mailbox=POSTFACH, enabled=True,
                       direction=SyncDirection.BOTH,
                       activated_at=datetime(2026, 6, 13, tzinfo=UTC))


def _fahrt_paar(support_id: int | None) -> Pair:
    """Fahrtblock in Outlook, Haupttermin nicht in dieser Runde."""
    key = SyncKey(POSTFACH, HAUPT_UID, -1, "travel_to")
    return Pair(
        tanss=None,
        graph=Appointment(key=key, kind=AppointmentKind.TRAVEL_TO, subject="Anfahrt",
                          start=datetime(2026, 9, 11, 9, 30, tzinfo=UTC),
                          end=datetime(2026, 9, 11, 10, 0, tzinfo=UTC),
                          graph_event_id="AAkALgAAAA"),
        link=LinkRecord(mailbox=POSTFACH, uid=HAUPT_UID, sequence=-1,
                        travel_role="travel_to", tanss_support_id=support_id,
                        graph_event_id="AAkALgAAAA", tanss_employee_id=1,
                        write_direction=SyncDirection.TANSS_TO_M365),
    )


def _reconciler() -> Reconciler:
    return Reconciler(SyncRules(SyncConfig()), EchoGuard(30))


def test_fahrt_folgt_dem_nachweislich_entfernten_haupttermin() -> None:
    changes = _reconciler().deletion_candidates(
        [_fahrt_paar(SUPPORT)], _user(),
        entfernte_haupttermine=frozenset({HAUPT_UID}))
    assert len(changes.actions) == 1, (
        "der Haupttermin ist nachweislich entfernt — die Fahrt gehört mit")
    assert changes.actions[0].appointment.key.travel_role == "travel_to"


def test_ohne_diesen_nachweis_bleibt_die_fahrt_unangetastet() -> None:
    """Der Schutz für ältere Termine bleibt: Fehlen allein belegt nichts."""
    changes = _reconciler().deletion_candidates([_fahrt_paar(SUPPORT)], _user())
    assert changes.actions == []
    assert changes.skipped, "und der Grund gehört ins Protokoll"
