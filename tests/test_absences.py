"""Abwesenheiten — Urlaub, Krankheit, Abwesenheit, Überstunden, Bereitschaft.

Sie gehen **nur** von TANSS nach Outlook. Der Grund ist nicht Bequemlichkeit: Ein
Urlaubstag ist in TANSS kein Termin, sondern ein bewilligter Antrag mit Kontingent.
Wer ihn über den Kalender zurückschreiben oder löschen ließe, veränderte einen
Vorgang der Personalverwaltung durch einen Mausklick in Outlook.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tanss_sync.config.models import SafetyConfig, SyncConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.change import SyncAction, SyncOperation
from tanss_sync.domain.identity import SyncDirection, SyncKey, UserMapping
from tanss_sync.microsoft.mapper import GraphMapper
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.deletion import DeletionGuard
from tanss_sync.sync.rules import SyncRules
from tanss_sync.util.timezone import TimeConverter

MAILBOX = "mitarbeiter@example.com"
START = dt.datetime(2026, 3, 2, 7, tzinfo=dt.UTC)

ABWESENHEITEN = [
    AppointmentKind.VACATION,
    AppointmentKind.ILLNESS,
    AppointmentKind.ABSENCE,
    AppointmentKind.STANDBY,
    AppointmentKind.CUSTOM,
]


def absence(kind: AppointmentKind = AppointmentKind.VACATION, *,
            day: int = 2, request_id: int | None = 321) -> Appointment:
    made = Appointment(key=SyncKey(MAILBOX, f"uid-{kind}-{day}", -1, "main"), kind=kind)
    made.subject = "Urlaub"
    made.start = START.replace(day=day)
    made.end = made.start + dt.timedelta(minutes=540)
    made.vacation_request_id = request_id
    made.tanss_support_id = 50000 + day
    return made


def user() -> UserMapping:
    return UserMapping(
        tanss_employee_id=42, tanss_name="Muster, Max", tanss_email=MAILBOX,
        mailbox=MAILBOX, enabled=True, direction=SyncDirection.BOTH,
        activated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))


# ----------------------------------------------------------------- nur eine Richtung

@pytest.mark.parametrize("kind", ABWESENHEITEN)
def test_abwesenheit_geht_nie_zurueck_nach_tanss(kind: AppointmentKind) -> None:
    assert not kind.syncs_to_tanss


@pytest.mark.parametrize("kind", ABWESENHEITEN)
def test_kopplung_einer_abwesenheit_ist_schreibgeschuetzt(kind: AppointmentKind) -> None:
    """Der Schutz wird abgeleitet, nicht von der Benutzereinstellung geerbt.

    Stünde hier ``user.direction``, wäre er beim Standard ``both`` still abgeschaltet.
    """
    link = LinkRecord.for_new(absence(kind), user())
    assert link.write_direction is SyncDirection.TANSS_TO_M365
    assert link.is_write_protected_in_tanss


@pytest.mark.parametrize("kind", ABWESENHEITEN)
def test_abwesenheit_geht_nach_outlook(kind: AppointmentKind) -> None:
    assert kind.syncs_to_outlook
    assert kind.is_absence


def test_abschaltbar_ueber_die_konfiguration() -> None:
    rules = SyncRules(SyncConfig(sync_absences=False))
    verdict = rules.should_sync_to_outlook(absence(), user(), created_filtered=True)
    assert not verdict
    assert "abgeschaltet" in verdict.reason


# ------------------------------------------------------------------ Darstellung

def test_abwesenheit_wird_ohne_erinnerung_angelegt() -> None:
    """Ein Urlaubsantrag über zwei Wochen sind zehn Einträge — und sonst zehn Erinnerungen."""
    payload = GraphMapper(TimeConverter()).to_create_payload(absence())
    assert payload["isReminderOn"] is False


def test_normaler_termin_behaelt_die_outlook_voreinstellung() -> None:
    """Bei einem echten Termin ist eine Erinnerung erwünscht — wir setzen sie nicht ab."""
    termin = absence(AppointmentKind.FIXED)
    payload = GraphMapper(TimeConverter()).to_create_payload(termin)
    assert "isReminderOn" not in payload


# ------------------------------------------------------------------ Not-Aus

def _deletions(count: int, *, request_id: int | None) -> list[SyncAction]:
    return [
        SyncAction(direction=SyncDirection.TANSS_TO_M365,
                   operation=SyncOperation.DELETE,
                   appointment=absence(day=day, request_id=request_id),
                   reason="Antrag zurückgezogen")
        for day in range(1, count + 1)
    ]


def test_ein_zurueckgezogener_urlaubsantrag_zaehlt_als_eine_operation(tmp_path) -> None:
    """Sonst hielte der Dienst bei jeder gewöhnlichen Urlaubsstornierung an.

    TANSS führt je Kalendertag einen Datensatz — ein zurückgezogener Dreiwochenurlaub
    sind fünfzehn Löschungen auf einen Schlag.
    """
    from tanss_sync.state.store import StateStore

    store = StateStore(tmp_path / "state.db")
    store.migrate()
    guard = DeletionGuard(SafetyConfig(max_deletes_per_run=10), store)

    verdict = guard.check_batch(_deletions(15, request_id=321), linked_total=500,
                                scope="user:42", allow_bulk=False)
    assert verdict.allowed, verdict.reason
    assert verdict.counted == 1

    store.close()


def test_viele_einzelne_loeschungen_loesen_den_not_aus_aus(tmp_path) -> None:
    """Ohne Antragsbezug bleibt jede Löschung eine eigene Operation."""
    from tanss_sync.state.store import StateStore

    store = StateStore(tmp_path / "state.db")
    store.migrate()
    guard = DeletionGuard(SafetyConfig(max_deletes_per_run=10), store)

    verdict = guard.check_batch(_deletions(15, request_id=None), linked_total=500,
                                scope="user:42", allow_bulk=False)
    assert not verdict.allowed
    assert verdict.counted == 15

    store.close()


# ------------------------------------------- Termine einer anderen Synchronisation

def test_fremd_betreuter_termin_wird_erkannt() -> None:
    termin = absence(AppointmentKind.FIXED)
    termin.categories = ["TANSS:60fab417-8ea8-45d6-aa3b-0ec1faf1c089"]
    assert SyncRules(SyncConfig()).already_owned_elsewhere(termin)


def test_gewoehnliche_kategorie_bleibt_folgenlos() -> None:
    termin = absence(AppointmentKind.FIXED)
    termin.categories = ["Wichtig", "Projekt Nord"]
    assert not SyncRules(SyncConfig()).already_owned_elsewhere(termin)
