"""Der Not-Aus bei Löschungen.

Zwei Grenzen wirken zusammen: eine absolute Zahl und ein Anteil an den verknüpften
Terminen. Der Anteil ist der heikle Teil — er braucht eine Untergrenze, sonst ist er
bei wenigen Verknüpfungen bedeutungslos.

Am Kundensystem aufgefallen: Ein Testbenutzer mit zwei gekoppelten Terminen löste beim
Löschen beider den Not-Aus aus, weil das 100 % sind. Mit einer einzigen Kopplung wäre
**jede** Löschung 100 % gewesen — der Dienst hätte bei jedem gewöhnlichen Vorgang
angehalten, und ein Alarm, der immer schrillt, wird abgeschaltet und schützt dann gar
nichts mehr.
"""

from __future__ import annotations

import pytest

from tanss_sync.config.models import SafetyConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.change import SyncAction, SyncOperation
from tanss_sync.domain.identity import SyncDirection, SyncKey
from tanss_sync.state.store import StateStore
from tanss_sync.sync.deletion import DeletionGuard

#: Die Mengenbremsen sind im Auslieferungszustand **aus** — sie hielten den Dienst bei
#: gewöhnlichen Vorgängen an. Die Tests hier schalten sie deshalb ausdrücklich ein; nur
#: ``test_bremsen_sind_ab_werk_aus`` prüft den Standard.
MIT_BREMSE = {"max_deletes_per_run": 10, "max_delete_ratio": 0.2}


@pytest.fixture
def guard(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    yield DeletionGuard(SafetyConfig(**MIT_BREMSE), store)
    store.close()


@pytest.fixture
def ohne_bremse(tmp_path):
    store = StateStore(tmp_path / "aus.db")
    store.migrate()
    yield DeletionGuard(SafetyConfig(), store)
    store.close()


def deletions(count: int, *, request_id: int | None = None) -> list[SyncAction]:
    out = []
    for i in range(count):
        appointment = Appointment(key=SyncKey("mb", f"uid{i}", -1, "main"),
                                  kind=AppointmentKind.FIXED)
        appointment.tanss_support_id = 1000 + i
        appointment.vacation_request_id = request_id
        out.append(SyncAction(direction=SyncDirection.M365_TO_TANSS,
                              operation=SyncOperation.DELETE,
                              appointment=appointment, reason="in Outlook entfernt"))
    return out


def check(guard: DeletionGuard, links: int, count: int, **kwargs):
    return guard.check_batch(deletions(count, **kwargs), links,
                             scope=f"user:{links}-{count}", allow_bulk=False)


# ------------------------------------------------- kleine Bestände, kein Fehlalarm

@pytest.mark.parametrize("links,count", [(1, 1), (2, 1), (2, 2), (5, 3), (10, 4)])
def test_wenige_verknuepfungen_loesen_nicht_aus(guard, links: int, count: int) -> None:
    """Unterhalb der Untergrenze sagt ein Anteil nichts — nur die Anzahl zählt."""
    assert check(guard, links, count).allowed


def test_einzelne_loeschung_bei_einer_einzigen_kopplung(guard) -> None:
    """100 % — und trotzdem völlig gewöhnlich."""
    verdict = check(guard, 1, 1)
    assert verdict.allowed
    assert verdict.counted == 1


# ------------------------------------------------------------ Anteil ab der Grenze

def test_anteil_greift_ab_der_untergrenze(guard) -> None:
    assert not check(guard, 20, 5).allowed  # 25 % von 20


def test_kleiner_anteil_laeuft_durch(guard) -> None:
    assert check(guard, 100, 5).allowed  # 5 %


def test_grosser_anteil_haelt_an(guard) -> None:
    verdict = check(guard, 100, 30)
    assert not verdict.allowed
    assert "30%" in verdict.reason


# ------------------------------------------------------------------ absolute Zahl

def test_zu_viele_auf_einmal_halten_an_auch_bei_kleinem_anteil(guard) -> None:
    """11 Löschungen von 1000 sind nur 1 % — aber elf auf einen Schlag sind elf."""
    assert not check(guard, 1000, 11).allowed


def test_genau_an_der_grenze_laeuft_noch_durch(guard) -> None:
    assert check(guard, 1000, 10).allowed


# ------------------------------------------------------------------ Urlaubsanträge

def test_ein_zurueckgezogener_urlaubsantrag_bleibt_ein_vorgang(guard) -> None:
    """TANSS führt je Kalendertag einen Datensatz — sonst hielte jede Stornierung an."""
    verdict = check(guard, 1000, 15, request_id=321)
    assert verdict.allowed
    assert verdict.counted == 1


# ------------------------------------------------------------------ Freigabe

def test_freigabe_laesst_auch_eine_massenloeschung_durch(guard) -> None:
    verdict = guard.check_batch(deletions(50), 100, scope="user:1", allow_bulk=True)
    assert verdict.allowed
    assert "freigegeben" in verdict.reason


# ------------------------------------------------------------------ Standard: aus

def test_bremsen_sind_ab_werk_aus(ohne_bremse) -> None:
    """0 bedeutet: keine Grenze.

    Was unabhängig davon trägt: der Nachweis am Einzelobjekt, die Karenzzeit und die
    Sicherung vor jeder Löschung. Die Mengenbremse ist eine Zusatzsicherung, kein
    Ersatz dafür — und eine, die bei gewöhnlichen Vorgängen mehr störte als half.
    """
    assert ohne_bremse.check_batch(deletions(500), 500, scope="user:1").allowed


def test_ohne_bremse_wird_kein_not_aus_ausgeloest(ohne_bremse) -> None:
    ohne_bremse.check_batch(deletions(500), 500, scope="user:7")
    assert ohne_bremse.state.active_emergency("user:7") is None


def test_bremse_laesst_sich_einschalten(tmp_path) -> None:
    store = StateStore(tmp_path / "an.db")
    store.migrate()
    streng = DeletionGuard(SafetyConfig(max_deletes_per_run=5), store)
    assert not streng.check_batch(deletions(6), 500, scope="user:1").allowed
    store.close()
