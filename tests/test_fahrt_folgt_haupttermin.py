"""Fahrtzeiten einer getätigten Leistung erscheinen nicht in Outlook.

``planning_type = SUPPORT`` ist in TANSS keine Terminplanung, sondern eine gebuchte
Leistung — ``tanss/mapper.py`` bildet sie deshalb auf ``WORK_LOG`` ab, und
``syncs_to_outlook`` verneint sie. Der Haupttermin wurde folgerichtig übersprungen,
seine Fahrtzeiten aber trotzdem projiziert.

Am Produktivsystem stand deshalb im Kalender von a.schulte am 02.09. eine Anfahrt
um 08:38 und eine Abfahrt um 13:35 — und dazwischen kein Termin. Vier der sechs
verbliebenen Überlappungen gingen darauf zurück.

Eine Fahrt ist eine Projektion ihres Haupttermins. Geht der nicht nach Outlook,
geht sie auch nicht.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tanss_sync.domain.appointment import (Appointment, AppointmentKind,
                                           ServiceLocation, TravelTime)
from tanss_sync.domain.identity import SyncKey


def _termin(kind: AppointmentKind) -> Appointment:
    return Appointment(
        key=SyncKey("a.schulte@pronet-systems.de", "pending:59025", -1, "main"),
        kind=kind,
        subject="(Firma: Perstorp Chemicals GmbH)",
        start=datetime(2026, 9, 2, 6, 50, tzinfo=UTC),
        end=datetime(2026, 9, 2, 11, 35, tzinfo=UTC),
        service_location=ServiceLocation.CUSTOMER,
        travel=TravelTime(minutes_before=12, minutes_after=12),
        tanss_support_id=59025,
    )


def test_getaetigte_leistung_projiziert_keine_fahrt() -> None:
    teile = _termin(AppointmentKind.WORK_LOG).split_travel()
    assert len(teile) == 1
    assert teile[0].key.travel_role == "main"


def test_unbekannte_art_projiziert_keine_fahrt() -> None:
    assert len(_termin(AppointmentKind.UNKNOWN).split_travel()) == 1


@pytest.mark.parametrize("kind", [AppointmentKind.FIXED, AppointmentKind.TENTATIVE])
def test_echter_termin_projiziert_weiterhin_beide_fahrten(kind) -> None:
    teile = _termin(kind).split_travel()
    assert [t.key.travel_role for t in teile] == ["travel_to", "main", "travel_back"]
    anfahrt, termin, abfahrt = teile
    assert anfahrt.end == termin.start
    assert abfahrt.start == termin.end
    assert termin.start - anfahrt.start == timedelta(minutes=12)
