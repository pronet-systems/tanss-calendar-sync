"""Eine Abwesenheit findet ihre Kopplung wieder.

``pair()`` erkennt eine bestehende Verknüpfung auf drei Wegen: über den
gespeicherten Schlüssel, über die kanonische UID und über die Kennung am
Outlook-Termin. Für Urlaub und Abwesenheit greift keiner davon.

Der Grund hängt an der Schreibrichtung: Die echte UID landet normalerweise per
``_write_back_coupling`` in den ``metaInfos`` des TANSS-Datensatzes, und von dort
holt sie der nächste Lauf. Urlaub und Abwesenheit hängen aber an einem
Urlaubsantrag und sind über die Support-Route nicht schreibbar — die TANSS-Seite
liefert deshalb dauerhaft den Platzhalter ``pending:<support_id>``.

Folge am Produktivsystem: TANSS-Seite und Outlook-Seite landeten in getrennten
Paaren, der Termin galt jeden Lauf erneut als Übernahmekandidat, und
``set_tanss_id`` schrieb alle 30 Sekunden dieselbe Kennung erneut nach Graph.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tanss_sync.config.models import SyncConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.identity import SyncDirection, SyncKey
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.echo import EchoGuard
from tanss_sync.sync.reconciler import Reconciler
from tanss_sync.sync.rules import SyncRules

POSTFACH = "n.schlagheck@pronet-systems.de"
ECHTE_UID = "040000008200E00074C5B7101A82E00800000000D0FCE461E341DD01"
GRAPH_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AHNomq68wdEaT1"
SUPPORT = 59091


def _reconciler() -> Reconciler:
    return Reconciler(SyncRules(SyncConfig()), EchoGuard(30))


def _urlaub_aus_tanss() -> Appointment:
    """Wie TANSS ihn liefert: ohne je zurueckgeschriebene UID, also mit Platzhalter."""
    return Appointment(
        key=SyncKey(POSTFACH, f"pending:{SUPPORT}", -1, "main"),
        kind=AppointmentKind.VACATION,
        subject="Urlaub",
        start=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        end=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        tanss_support_id=SUPPORT,
    )


def _urlaub_aus_outlook() -> Appointment:
    return Appointment(
        key=SyncKey(POSTFACH, ECHTE_UID, -1, "main"),
        kind=AppointmentKind.VACATION,
        subject="Urlaub",
        start=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        end=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        graph_event_id=GRAPH_ID,
    )


def _kopplung() -> LinkRecord:
    return LinkRecord(
        mailbox=POSTFACH, uid=ECHTE_UID, sequence=-1, travel_role="main",
        tanss_support_id=SUPPORT, graph_event_id=GRAPH_ID,
        tanss_employee_id=1299, write_direction=SyncDirection.TANSS_TO_M365,
    )


def test_urlaub_findet_seine_kopplung_ueber_die_support_kennung() -> None:
    pairs = _reconciler().pair([_urlaub_aus_tanss()], [_urlaub_aus_outlook()],
                               [_kopplung()])

    assert len(pairs) == 1, "TANSS- und Outlook-Seite gehören in dasselbe Paar"
    paar = pairs[0]
    assert paar.tanss is not None
    assert paar.graph is not None
    assert paar.link is not None, "die bestehende Kopplung muss gefunden werden"


def test_ohne_platzhalter_bleibt_die_paarung_unveraendert() -> None:
    """Traegt die TANSS-Seite eine echte UID, entscheidet weiterhin sie."""
    tanss = _urlaub_aus_tanss()
    tanss.key = SyncKey(POSTFACH, ECHTE_UID, -1, "main")
    pairs = _reconciler().pair([tanss], [_urlaub_aus_outlook()], [_kopplung()])
    assert len(pairs) == 1
    assert pairs[0].link is not None
