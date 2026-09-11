"""Urlaub und Abwesenheit bekommen keine Kopplung zurückgeschrieben.

Am Produktivsystem aufgefallen: Der Dienst meldete alle 30 Sekunden erneut

    Kopplung für Support 59091 nicht zurückgeschrieben: Objekt nicht gefunden

für drei Datensätze — 59091 und 59360 (Urlaub), 59253 (Abwesenheit). Lesen
funktioniert, ``get_support`` liefert sie. Schreiben nicht: Sie hängen an einem
Urlaubsantrag (``vacation_request_id``) und sind über die Support-Route nicht
änderbar.

Der Versuch ist damit nicht bloß erfolglos, sondern grundsätzlich aussichtslos.
Er gehört unterlassen, nicht abgefangen — sonst wächst das Protokoll für jeden
Urlaubstag jedes Mitarbeiters unbegrenzt weiter und verdeckt echte Fehler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tanss_sync.config.models import AppConfig
from tanss_sync.domain.appointment import Appointment, AppointmentKind
from tanss_sync.domain.identity import SyncKey
from tanss_sync.state.store import StateStore
from tanss_sync.sync.engine import SyncEngine

KONFIG = {
    "version": 1,
    "tanss": {"base_url": "https://example.invalid/backend",
              "token_ref": "file:/tmp/kein-token", "token_owner_employee_id": 1},
    "microsoft": {"tenant_id": "t", "client_id": "c",
                  "auth": {"mode": "secret", "client_secret_ref": "env:KEIN_SECRET"}},
    "users": [],
}


class TanssAttrappe:
    """Zählt mit, ob geschrieben wurde."""

    def __init__(self) -> None:
        self.gelesen: list[int] = []
        self.geschrieben: list[int] = []

    def get_support(self, support_id: int):
        self.gelesen.append(support_id)
        raise AssertionError("Für eine Abwesenheit darf gar nicht erst gelesen werden")

    def update_support(self, support_id: int, write) -> None:
        self.geschrieben.append(support_id)


def _engine(tmp_path) -> tuple[SyncEngine, TanssAttrappe]:
    state = StateStore(tmp_path / "state.db")
    state.migrate()
    tanss = TanssAttrappe()
    return SyncEngine(AppConfig.model_validate(KONFIG), tanss, None, state), tanss


def _termin(kind: AppointmentKind) -> Appointment:
    return Appointment(
        key=SyncKey("n.schlagheck@pronet-systems.de", "uid-1", -1, "main"),
        kind=kind,
        subject="Urlaub",
        start=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        end=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        tanss_support_id=59091,
    )


@pytest.mark.parametrize("kind", [AppointmentKind.VACATION,
                                  AppointmentKind.ABSENCE,
                                  AppointmentKind.ILLNESS,
                                  AppointmentKind.OVERTIME])
def test_abwesenheit_wird_nicht_zurueckgeschrieben(tmp_path, kind) -> None:
    engine, tanss = _engine(tmp_path)
    engine._write_back_coupling(_termin(kind))
    assert tanss.gelesen == []
    assert tanss.geschrieben == []


def test_gewoehnlicher_termin_schreibt_weiterhin_zurueck(tmp_path) -> None:
    """Der Normalfall darf durch den Wächter nicht mit abgeschaltet werden."""
    engine, tanss = _engine(tmp_path)
    with pytest.raises(AssertionError, match="gar nicht erst gelesen"):
        engine._write_back_coupling(_termin(AppointmentKind.FIXED))
    assert tanss.gelesen == [59091]
