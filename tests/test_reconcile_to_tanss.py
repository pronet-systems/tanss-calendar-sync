"""Der Rückweg nach TANSS — und vor allem, wann er **nicht** schreibt.

In TANSS hängen an einem Termin Leistungen, Tickets und Abwesenheitsanträge. Ein zu
Unrecht geschriebener oder gelöschter Datensatz kostet dort mehr als ein überflüssiger
Kalendereintrag. Die Tests hier prüfen deshalb überwiegend Sperren.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tanss_sync.config.models import SyncConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.change import SyncOperation
from tanss_sync.domain.identity import SyncDirection, SyncKey, UserMapping
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.compare import fingerprint
from tanss_sync.sync.echo import EchoGuard
from tanss_sync.sync.reconciler import Pair, Reconciler
from tanss_sync.sync.rules import SyncRules

MAILBOX = "mitarbeiter@example.com"
UID = "termin-uid-1"
START = dt.datetime(2026, 3, 2, 9, tzinfo=dt.UTC)


def build(conflict_winner: str = "tanss") -> Reconciler:
    config = SyncConfig(conflict_winner=conflict_winner)
    return Reconciler(SyncRules(config), EchoGuard(0))


def user(direction: SyncDirection = SyncDirection.BOTH) -> UserMapping:
    return UserMapping(
        tanss_employee_id=42, tanss_name="Muster, Max", tanss_email=MAILBOX,
        mailbox=MAILBOX, enabled=True, direction=direction,
        activated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))


def appointment(*, subject: str = "Besprechung", minutes: int = 60,
                kind: AppointmentKind = AppointmentKind.FIXED,
                show_as: str = "busy", all_day: bool = False,
                cancelled: bool = False, event_id: str | None = "EV1",
                support_id: int | None = 4711) -> Appointment:
    made = Appointment(key=SyncKey(MAILBOX, UID, -1, "main"), kind=kind)
    made.subject = subject
    made.start = START
    made.end = START + dt.timedelta(minutes=minutes)
    made.show_as = show_as
    made.all_day = all_day
    made.is_cancelled = cancelled
    made.graph_event_id = event_id
    made.tanss_support_id = support_id
    made.created = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return made


def link(*, write_direction: SyncDirection = SyncDirection.BOTH,
         travel_role: str = "main", state: str = "linked",
         support_id: int | None = 4711,
         tanss_hash: str | None = None,
         graph_hash: str | None = None) -> LinkRecord:
    return LinkRecord(
        mailbox=MAILBOX, uid=UID, sequence=-1, travel_role=travel_role,
        tanss_support_id=support_id, graph_event_id="EV1", tanss_employee_id=42,
        state=state, write_direction=write_direction,
        last_hash_tanss=tanss_hash, last_hash_graph=graph_hash)


def operations(changes) -> list[SyncOperation]:
    return [a.operation for a in changes.actions]


# ------------------------------------------------------------------ Schreibschutz

@pytest.mark.parametrize("role", ["main", "travel_to"])
def test_schreibgeschuetzte_kopplung_erzeugt_nie_eine_aktion(role: str) -> None:
    """Sonst löschte ein in Outlook entfernter Urlaubstag den echten TANSS-Urlaub."""
    pair = Pair(tanss=appointment(), graph=appointment(subject="Urlaub"),
                link=link(write_direction=SyncDirection.TANSS_TO_M365,
                          travel_role=role))
    changes = build().reconcile_to_tanss([pair], user())
    assert changes.actions == []
    assert "schreibgeschützt" in changes.skipped[0][1]


def test_richtung_gesperrt_schreibt_nicht() -> None:
    pair = Pair(tanss=None, graph=appointment(), link=None)
    changes = build().reconcile_to_tanss([pair], user(SyncDirection.TANSS_TO_M365))
    assert changes.actions == []


# ---------------------------------------------------------- entkoppeln statt löschen

@pytest.mark.parametrize("kwargs", [
    {"show_as": "free"},
    {"all_day": True},
    {"cancelled": True},
])
def test_unsynchronisierbar_wird_entkoppelt_nicht_geloescht(kwargs: dict) -> None:
    """Auf Frei setzen darf kein stiller Löschbefehl für den TANSS-Datensatz sein."""
    pair = Pair(tanss=appointment(), graph=appointment(**kwargs), link=link())
    changes = build().reconcile_to_tanss([pair], user())
    assert operations(changes) == [SyncOperation.DETACH]


# ------------------------------------------------------------------------- anlegen

def test_nur_in_outlook_wird_in_tanss_angelegt() -> None:
    pair = Pair(tanss=None, graph=appointment(support_id=None), link=None)
    changes = build().reconcile_to_tanss([pair], user())
    assert operations(changes) == [SyncOperation.CREATE]


def test_bereits_gekoppelt_wird_nie_angelegt() -> None:
    """Dass das TANSS-Gegenstück fehlt, heißt nicht, dass es gelöscht wurde."""
    pair = Pair(tanss=None, graph=appointment(), link=link())
    changes = build().reconcile_to_tanss([pair], user())
    assert changes.actions == []
    assert "nicht sichtbar" in changes.skipped[0][1]


# ------------------------------------------------------------------------- ändern

def test_ohne_ausgangsmarke_wird_nur_uebernommen() -> None:
    """Der erste Abgleich merkt sich den Stand, statt ihn zu schreiben."""
    pair = Pair(tanss=appointment(), graph=appointment(subject="Anders"), link=link())
    changes = build().reconcile_to_tanss([pair], user())
    assert changes.actions == []
    assert len(changes.baselines) == 1


def test_aenderung_in_outlook_wird_uebertragen() -> None:
    tanss_side = appointment()
    graph_side = appointment(subject="Neuer Betreff")
    pair = Pair(tanss=tanss_side, graph=graph_side,
                link=link(tanss_hash=fingerprint(tanss_side), graph_hash="veraltet"))
    changes = build().reconcile_to_tanss([pair], user())
    assert operations(changes) == [SyncOperation.UPDATE]
    assert changes.actions[0].appointment.tanss_support_id == 4711


def test_konflikt_ohne_gewinner_outlook_schreibt_nicht() -> None:
    """Schrieben beide Richtungen, überschrieben sie sich bei jedem Lauf gegenseitig."""
    pair = Pair(tanss=appointment(subject="TANSS-Fassung"),
                graph=appointment(subject="Outlook-Fassung"),
                link=link(tanss_hash="alt", graph_hash="alt"))
    changes = build(conflict_winner="tanss").reconcile_to_tanss([pair], user())
    assert changes.actions == []


def test_konflikt_mit_gewinner_outlook_schreibt() -> None:
    pair = Pair(tanss=appointment(subject="TANSS-Fassung"),
                graph=appointment(subject="Outlook-Fassung"),
                link=link(tanss_hash="alt", graph_hash="alt"))
    changes = build(conflict_winner="outlook").reconcile_to_tanss([pair], user())
    assert operations(changes) == [SyncOperation.UPDATE]


# ------------------------------------------------------------------------ löschen

def test_loeschverdacht_braucht_eine_kopplung() -> None:
    pair = Pair(tanss=appointment(), graph=None, link=None)
    assert build().deletion_candidates([pair], user()).actions == []


def test_loeschverdacht_wird_bei_schreibschutz_nie_erhoben() -> None:
    """Eine Abwesenheit kommt gar nicht erst in die Nähe des Löschpfads."""
    pair = Pair(tanss=appointment(kind=AppointmentKind.VACATION), graph=None,
                link=link(write_direction=SyncDirection.TANSS_TO_M365))
    assert build().deletion_candidates([pair], user()).actions == []


def test_loeschverdacht_bei_fehlendem_outlook_termin() -> None:
    pair = Pair(tanss=appointment(), graph=None, link=link())
    candidates = build().deletion_candidates([pair], user())
    assert operations(candidates) == [SyncOperation.DELETE]
    assert candidates.actions[0].direction is SyncDirection.M365_TO_TANSS


def test_loeschverdacht_bei_fehlendem_tanss_termin() -> None:
    pair = Pair(tanss=None, graph=appointment(), link=link())
    candidates = build().deletion_candidates([pair], user())
    assert operations(candidates) == [SyncOperation.DELETE]
    assert candidates.actions[0].direction is SyncDirection.TANSS_TO_M365


def test_beendete_kopplung_erzeugt_keinen_loeschverdacht() -> None:
    pair = Pair(tanss=appointment(), graph=None, link=link(state="detached"))
    assert build().deletion_candidates([pair], user()).actions == []
