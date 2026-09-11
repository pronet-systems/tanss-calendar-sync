"""Ein Not-Aus erlischt, wenn der Anlass vorbei ist.

``release_emergency`` wird nur erreicht, wenn ein Lauf eine Grenze **überschreitet**
und ausdrücklich freigegeben wird (``sync/deletion.py``, Zweig ``allow_bulk``). Damit
war ein ausgelöster Not-Aus nicht mehr loszuwerden, sobald die Mengenbremsen wieder
auf ihrem Auslieferungswert 0 standen: Die Prüfung kehrt dann vorher zurück, und
``status`` wie ``doctor`` meldeten den Alarm dauerhaft weiter.

Am Produktivsystem genau so eingetreten — der Alarm blieb stehen, obwohl die
Löschungen längst freigegeben und ausgeführt waren. ``tanss-sync ack`` schweigt den
Alarm, hebt ihn aber bewusst nicht auf.

Ein Alarm, der immer schrillt, wird abgeschaltet und schützt dann gar nichts mehr —
dieselbe Überlegung wie bei der Untergrenze der Anteilsprüfung.
"""

from __future__ import annotations

import pytest

from tanss_sync.config.models import SafetyConfig
from tanss_sync.state.store import StateStore
from tanss_sync.sync.deletion import DeletionGuard
from tests.test_deletion_guard import deletions


@pytest.fixture
def wache(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    yield DeletionGuard(SafetyConfig(max_deletes_per_run=5), store), store
    store.close()


def test_ein_ruhiger_lauf_beendet_den_notaus(wache) -> None:
    guard, store = wache
    assert not guard.check_batch(deletions(9), 100, scope="user:7").allowed
    assert store.active_emergency("user:7") is not None

    # Der nächste Lauf bleibt innerhalb der Grenze — der Anlass ist vorbei.
    assert guard.check_batch(deletions(2), 100, scope="user:7").allowed
    assert store.active_emergency("user:7") is None


def test_ein_weiterhin_zu_grosser_lauf_haelt_ihn_am_leben(wache) -> None:
    guard, store = wache
    assert not guard.check_batch(deletions(9), 100, scope="user:7").allowed
    assert not guard.check_batch(deletions(9), 100, scope="user:7").allowed
    assert store.active_emergency("user:7") is not None, "der Anlass besteht fort"


def test_abgeschaltete_bremse_laesst_den_alarm_nicht_stehen(tmp_path) -> None:
    """Der Fall vom Produktivsystem: Grenzen wieder auf 0, Alarm von vorher."""
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    streng = DeletionGuard(SafetyConfig(max_deletes_per_run=3), store)
    assert not streng.check_batch(deletions(9), 100, scope="user:1809").allowed

    ohne_bremse = DeletionGuard(SafetyConfig(), store)   # Auslieferungszustand: aus
    assert ohne_bremse.check_batch(deletions(9), 100, scope="user:1809").allowed
    assert store.active_emergency("user:1809") is None
    store.close()


def test_ein_anderer_mitarbeiter_bleibt_unberuehrt(wache) -> None:
    guard, store = wache
    assert not guard.check_batch(deletions(9), 100, scope="user:7").allowed
    guard.check_batch(deletions(1), 100, scope="user:8")
    assert store.active_emergency("user:7") is not None


def test_ein_lauf_ganz_ohne_loeschungen_beendet_ihn_auch(wache) -> None:
    """Der eigentliche Fall vom Produktivsystem: Es wird überhaupt nichts gelöscht."""
    guard, store = wache
    assert not guard.check_batch(deletions(9), 100, scope="user:7").allowed
    assert guard.check_batch([], 100, scope="user:7").allowed
    assert store.active_emergency("user:7") is None


def test_ein_notaus_wegen_neuanlagen_bleibt_stehen(wache) -> None:
    """Dass nichts gelöscht wurde, sagt über zu viele Neuanlagen nichts."""
    guard, store = wache
    store.raise_emergency(scope="user:9", kind="bulk_create", counted=40,
                          threshold=5, reason="40 Neuanlagen in einem Lauf")
    assert guard.check_batch([], 100, scope="user:9").allowed
    assert store.active_emergency("user:9") is not None
