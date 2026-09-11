"""Terminserien und Fahrtzeiten — die beiden Fälle, in denen ein Termin in TANSS
und in Outlook nicht dieselbe Form hat.

Eine Fahrtzeit ist in TANSS eine Zahl am Termin, in Outlook ein eigener Block. Eine
Serie ist in TANSS eine Regel mit berechneten, datensatzlosen Occurrences, in Outlook
eine Reihe echter Termine. Beides ist eine Einladung zur Duplikat-Lawine: Wer die Form
der einen Seite auf die andere überträgt, ohne die Identität zu klären, legt bei jedem
Lauf alles neu an.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tanss_sync.config.models import SyncConfig
from tanss_sync.domain.appointment import (
    Appointment,
    AppointmentKind,
    ServiceLocation,
    TravelTime,
)
from tanss_sync.domain.change import SyncOperation
from tanss_sync.domain.identity import SyncDirection, SyncKey, UserMapping
from tanss_sync.domain.uid import occurrence_sequence
from tanss_sync.microsoft.mapper import GraphMapper
from tanss_sync.state.records import LinkRecord
from tanss_sync.sync.adoption import find_adoption
from tanss_sync.sync.echo import EchoGuard
from tanss_sync.sync.reconciler import Pair, Reconciler
from tanss_sync.sync.rules import SyncRules
from tanss_sync.util.timezone import TimeConverter

MAILBOX = "mitarbeiter@example.com"
UID = "serien-uid"
START = dt.datetime(2026, 3, 2, 9, tzinfo=dt.UTC)


def user() -> UserMapping:
    return UserMapping(
        tanss_employee_id=42, tanss_name="Muster, Max", tanss_email=MAILBOX,
        mailbox=MAILBOX, enabled=True, direction=SyncDirection.BOTH,
        activated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))


def on_site(*, before: int = 30, after: int = 30) -> Appointment:
    """Ein Vor-Ort-Termin mit An- und Abfahrt."""
    made = Appointment(key=SyncKey(MAILBOX, UID, -1, "main"),
                       kind=AppointmentKind.FIXED)
    made.subject = "Umbau Lancom (Firma: Forstbetrieb)"
    made.location = "Ardeyer Str. 100, 58730 Fröndenberg"
    made.start = START
    made.end = START + dt.timedelta(minutes=60)
    made.service_location = ServiceLocation.CUSTOMER
    made.travel = TravelTime(minutes_before=before, minutes_after=after)
    made.tanss_support_id = 4711
    return made


def outlook_block(subject: str, start: dt.datetime, minutes: int,
                  event_id: str) -> Appointment:
    made = Appointment(key=SyncKey(MAILBOX, f"eigene-uid-{event_id}", -1, "main"))
    made.subject = subject
    made.start = start
    made.end = start + dt.timedelta(minutes=minutes)
    made.graph_event_id = event_id
    return made


# ------------------------------------------------------------------ Fahrtzeiten

def test_zerlegung_ergibt_anfahrt_termin_abfahrt() -> None:
    teile = on_site().split_travel()
    assert [t.key.travel_role for t in teile] == ["travel_to", "main", "travel_back"]
    anfahrt, termin, abfahrt = teile
    assert anfahrt.end == termin.start
    assert abfahrt.start == termin.end


def test_fahrt_bloecke_heissen_wie_im_bestand() -> None:
    """Ein abweichender Titel verhinderte, dass vorhandene Blöcke wiedererkannt werden."""
    anfahrt, _, abfahrt = on_site().split_travel()
    assert anfahrt.subject == "Anfahrt"
    assert abfahrt.subject == "Abfahrt"


def test_fahrt_bloecke_tragen_die_kundenadresse() -> None:
    """Ohne Adresse ist ein Fahrtblock im Kalender wertlos."""
    anfahrt, _, _ = on_site().split_travel()
    assert anfahrt.location == "Ardeyer Str. 100, 58730 Fröndenberg"


def test_alle_drei_teilen_sich_die_support_kennung() -> None:
    """Genau deshalb darf ein gelöschter Fahrt-Block nie auf TANSS durchschlagen."""
    assert {t.tanss_support_id for t in on_site().split_travel()} == {4711}


@pytest.mark.parametrize("ort", [ServiceLocation.OFFICE, ServiceLocation.REMOTE])
def test_ohne_vor_ort_termin_keine_fahrt(ort: ServiceLocation) -> None:
    """TANSS blockt Fahrtzeit ausschließlich bei Vor-Ort-Terminen."""
    termin = on_site()
    termin.service_location = ort
    assert len(termin.split_travel()) == 1


def test_nur_eine_richtung_ist_moeglich() -> None:
    teile = on_site(after=0).split_travel()
    assert [t.key.travel_role for t in teile] == ["travel_to", "main"]


def test_fahrt_kopplung_ist_tanss_seitig_gesperrt() -> None:
    anfahrt = on_site().split_travel()[0]
    link = LinkRecord.for_new(anfahrt, user())
    assert link.write_direction is SyncDirection.TANSS_TO_M365
    link.assert_consistent()


def test_fahrt_block_wird_ohne_erinnerung_angelegt() -> None:
    anfahrt = on_site().split_travel()[0]
    payload = GraphMapper(TimeConverter()).to_create_payload(anfahrt)
    assert payload["isReminderOn"] is False


# ---------------------------------------------- Übernahme über die Terminkante

def test_bestehender_fahrt_block_wird_trotz_anderer_dauer_uebernommen() -> None:
    """Eine Anfahrt endet, wenn der Termin beginnt. Die Dauer ist das, was sich ändert."""
    anfahrt = on_site(before=30).split_travel()[0]
    bestand = outlook_block("Anfahrt", START - dt.timedelta(minutes=12), 12, "EV1")
    gefunden = find_adoption(anfahrt, [bestand])
    assert gefunden is not None and gefunden.candidate is bestand


def test_abfahrt_wird_ueber_ihren_beginn_erkannt() -> None:
    abfahrt = on_site().split_travel()[-1]
    bestand = outlook_block("Abfahrt", abfahrt.start, 12, "EV2")
    assert find_adoption(abfahrt, [bestand]) is not None


def test_fahrt_block_an_anderer_kante_wird_nicht_uebernommen() -> None:
    anfahrt = on_site().split_travel()[0]
    fremd = outlook_block("Anfahrt", START + dt.timedelta(hours=5), 30, "EV3")
    assert find_adoption(anfahrt, [fremd]) is None


# ------------------------------------------- Fahrt-Blöcke an gekoppelten Terminen

def gekoppelt() -> Appointment:
    """Ein Vor-Ort-Termin, der in Outlook schon existiert — mit echter Kopplungs-UID."""
    termin = on_site()
    termin.key = SyncKey(MAILBOX, "040000008200E00074C5B7101A82E008-echt", -1, "main")
    return termin


def test_fahrt_block_entsteht_auch_an_einem_gekoppelten_termin() -> None:
    """Am Kundensystem übersehen: Der Fahrt-Block teilt sich die UID des Haupttermins.

    Die Regel „was bereits eine Kopplungs-UID trägt, wird nie angelegt" schützt vor
    Duplikaten — für die Hauptzeile. Auf den Fahrt-Block angewandt verhindert sie, dass
    je einer entsteht, sobald ein Termin einmal abgeglichen wurde. Und das ist der
    Normalfall.
    """
    anfahrt = gekoppelt().split_travel()[0]
    changes = build().reconcile_to_outlook(
        [Pair(tanss=anfahrt, graph=None, link=None)], user(), created_filtered=True)
    assert [a.operation for a in changes.actions] == [SyncOperation.CREATE]


def test_haupttermin_ohne_gegenstueck_wird_weiterhin_nicht_angelegt() -> None:
    """Die Regel selbst bleibt — sonst verdoppelt jeder unsichtbare Serientermin."""
    changes = build().reconcile_to_outlook(
        [Pair(tanss=gekoppelt(), graph=None, link=None)], user(), created_filtered=True)
    assert changes.actions == []
    assert "nicht sichtbar" in changes.skipped[0][1]


def test_entfernter_fahrt_block_wird_wieder_angelegt() -> None:
    """Er ist eine Projektion der Fahrtzeit in TANSS — und die steht noch."""
    anfahrt = gekoppelt().split_travel()[0]
    link = LinkRecord.for_new(anfahrt, user())
    link.graph_event_id = "war-mal-da"
    changes = build().reconcile_to_outlook(
        [Pair(tanss=anfahrt, graph=None, link=link)], user(), created_filtered=True)
    assert [a.operation for a in changes.actions] == [SyncOperation.CREATE]


# ----------------------------------- Der Schlüssel eines Fahrt-Blocks bleibt stabil

def test_fahrt_block_behaelt_die_uid_des_haupttermins() -> None:
    """Er wird aus TANSS bei jedem Lauf mit **dieser** UID neu gebildet.

    Übernähme man beim Anlegen die eigene UID des Outlook-Blocks in den Schlüssel,
    fände ihn der nächste Lauf nicht wieder: Er verknüpfte ihn erneut, und die alte
    Verknüpfung stünde ohne Gegenstück da — also im Löschpfad. Am Kundensystem
    beobachtet: Der gerade angelegte Anfahrt-Block war beim Folgelauf zur Löschung
    vorgemerkt.
    """
    termin = gekoppelt()
    for teil in termin.split_travel():
        assert teil.key.uid == termin.key.uid


def test_alle_drei_zeilen_teilen_uid_und_kennung() -> None:
    """Genau deshalb darf eine Fahrt-Zeile nie ihre Kopplung nach TANSS schreiben."""
    teile = gekoppelt().split_travel()
    assert len({t.key.uid for t in teile}) == 1
    assert len({t.tanss_support_id for t in teile}) == 1
    assert [t.key.travel_role for t in teile] == ["travel_to", "main", "travel_back"]


class MitschreibenderSpeicher:
    """Merkt sich, für welche Termine eine Kopplung zurückgeschrieben würde."""

    def __init__(self) -> None:
        self.geschrieben: list[int] = []

    def get_support(self, support_id: int):
        from tanss_sync.tanss.models import TanssSupport

        return TanssSupport.model_validate({
            "id": support_id, "date": 0, "duration": 0, "employeeId": 1,
            "companyId": 0, "planningType": "APPOINTMENT_FIX"})

    def update_support(self, support_id: int, write) -> None:
        self.geschrieben.append(support_id)


def test_nur_die_hauptzeile_schreibt_die_kopplung_zurueck() -> None:
    """Sonst landet die UID des Fahrt-Blocks in der ``SYNC_GROUP`` des Haupttermins.

    Am Kundensystem genau so passiert: Der Haupttermin zeigte danach auf den
    Anfahrt-Block, und der Abgleich verlor die Paarung für beide.
    """
    from tanss_sync.config.models import AppConfig
    from tanss_sync.sync.engine import SyncEngine

    config = AppConfig.model_validate({
        "tanss": {"base_url": "https://x/backend", "token_ref": "file:token",
                  "token_owner_employee_id": 1},
        "microsoft": {"tenant_id": "t", "client_id": "c",
                      "auth": {"mode": "secret", "client_secret_ref": "file:s"}},
    })
    speicher = MitschreibenderSpeicher()
    engine = SyncEngine.__new__(SyncEngine)
    engine.config = config
    engine.tanss = speicher
    engine.tanss_mapper = __import__(
        "tanss_sync.tanss.mapper", fromlist=["TanssMapper"]).TanssMapper(TimeConverter())

    anfahrt, haupt, _ = gekoppelt().split_travel()
    engine._write_back_coupling(anfahrt)
    assert speicher.geschrieben == [], "eine Fahrt-Zeile darf nie zurückschreiben"

    engine._write_back_coupling(haupt)
    assert speicher.geschrieben == [4711]


# ------------------------------------------------------------------ Serien

def test_occurrence_wird_ueber_ihren_zeitpunkt_bestimmt() -> None:
    """Beide Seiten müssen denselben Schlüssel bilden — TANSS nummeriert, Graph nicht."""
    genau = dt.datetime(2026, 5, 19, 14, 0, tzinfo=dt.UTC)
    mit_sekunden = genau.replace(second=37)
    assert occurrence_sequence(genau) == occurrence_sequence(mit_sekunden)
    assert occurrence_sequence(genau + dt.timedelta(minutes=1)) != occurrence_sequence(genau)


def occurrence(*, graph_id: str | None = None) -> Appointment:
    made = Appointment(key=SyncKey(MAILBOX, UID, occurrence_sequence(START), "main"),
                       kind=AppointmentKind.FIXED)
    made.subject = "Jour fixe"
    made.start = START
    made.end = START + dt.timedelta(minutes=15)
    made.is_occurrence = True
    made.graph_event_id = graph_id
    made.created = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return made


def build() -> Reconciler:
    return Reconciler(SyncRules(SyncConfig()), EchoGuard(0))


def test_einzelne_occurrence_wird_nicht_in_outlook_angelegt() -> None:
    """In Outlook gibt es sie nur als Teil ihrer Serie — sonst entstünden lose Einzeltermine."""
    changes = build().reconcile_to_outlook(
        [Pair(tanss=occurrence(), graph=None, link=None)], user(), created_filtered=True)
    assert changes.actions == []
    assert "Occurrence wird nicht angelegt" in changes.skipped[0][1]


def test_einzelne_occurrence_wird_nicht_in_tanss_angelegt() -> None:
    """In TANSS hängt sie an einer Regel und hat nicht einmal eine eigene Kennung."""
    changes = build().reconcile_to_tanss(
        [Pair(tanss=None, graph=occurrence(graph_id="EV9"), link=None)], user())
    assert changes.actions == []
    assert "Occurrence wird nicht angelegt" in changes.skipped[0][1]


def test_aenderung_an_einer_occurrence_wird_nicht_nach_tanss_geschrieben() -> None:
    """Eine virtuelle Occurrence trägt die Kennung 0 — es gibt keinen Datensatz dafür."""
    tanss_seite = occurrence()
    graph_seite = occurrence(graph_id="EV9")
    graph_seite.subject = "Jour fixe (verschoben)"
    link = LinkRecord(mailbox=MAILBOX, uid=UID, sequence=tanss_seite.key.sequence,
                      tanss_employee_id=42, graph_event_id="EV9",
                      last_hash_tanss="alt", last_hash_graph="aelter")
    changes = build().reconcile_to_tanss(
        [Pair(tanss=tanss_seite, graph=graph_seite, link=link)], user())
    assert changes.actions == []
    assert "kein eigener Datensatz" in changes.skipped[0][1]
