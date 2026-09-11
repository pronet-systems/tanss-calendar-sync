"""Die UID einer Verknüpfung wechselt, der Outlook-Termin bleibt derselbe.

Am Produktivsystem aufgefallen: Beim Anlegen eines Fahrt-Blocks trägt die Zeile
zunächst eine Platzhalter-UID (``pending:<support_id>``) — die echte ``iCalUId``
kennt erst der Server, nachdem der Termin existiert. Im nächsten Lauf schreibt der
Abgleich dieselbe Kopplung mit der **echten** UID fort.

Damit ändert sich der Primärschlüssel, ``ON CONFLICT(mailbox, uid, sequence,
travel_role)`` greift also nicht mehr — und das INSERT läuft stattdessen in den
zweiten Index ``idx_links_event(mailbox, graph_event_id)``. Der Dienst meldete das
alle 30 Sekunden erneut, für jeden betroffenen Termin, ohne je fertig zu werden.
"""

from __future__ import annotations

from tanss_sync.domain.identity import SyncDirection
from tanss_sync.state.records import LinkRecord
from tanss_sync.state.store import StateStore

GRAPH_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AHNomq68wdEaT1"
ECHTE_UID = "040000008200E00074C5B7101A82E00800000000D0FCE461E341DD01"


def _zeile(uid: str) -> LinkRecord:
    return LinkRecord(
        mailbox="n.schlagheck@pronet-systems.de",
        uid=uid,
        sequence=-1,
        travel_role="travel_to",
        tanss_support_id=59353,
        graph_event_id=GRAPH_ID,
        tanss_employee_id=1299,
        write_direction=SyncDirection.TANSS_TO_M365,
    )


def test_platzhalter_uid_weicht_der_echten(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    store.upsert_link(_zeile("pending:59353"))

    # Zweiter Lauf: derselbe Outlook-Termin, jetzt mit seiner echten iCalUId.
    store.upsert_link(_zeile(ECHTE_UID))

    rows = store.connect().execute(
        "SELECT uid FROM links WHERE mailbox=? AND graph_event_id=?",
        ("n.schlagheck@pronet-systems.de", GRAPH_ID),
    ).fetchall()

    # Ein Outlook-Termin gehört genau einer Verknüpfung — und zwar der echten.
    assert [r[0] for r in rows] == [ECHTE_UID]


def test_gleiche_uid_bleibt_ein_gewoehnliches_fortschreiben(tmp_path) -> None:
    """Der Normalfall darf sich durch die Aufräumlogik nicht ändern."""
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    store.upsert_link(_zeile(ECHTE_UID))
    store.upsert_link(_zeile(ECHTE_UID))

    rows = store.connect().execute(
        "SELECT COUNT(*) FROM links WHERE graph_event_id=?", (GRAPH_ID,)
    ).fetchone()
    assert rows[0] == 1
