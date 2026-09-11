"""Event-Regeln: Duplikate erkennen, fremde Meldewege unterscheiden.

Die Regeln sind der Grund, warum ein Parallelbetrieb zweier Terminsynchronisationen
auffliegt — und die Duplikaterkennung hat einen Haken, der in echten Daten sofort
zuschlägt: Jede Regel trägt eine eigene Kennung in ihrer Rückrufadresse. Wer nach der
vollständigen Adresse gruppiert, findet deshalb nie ein Duplikat.
"""

from __future__ import annotations

from tanss_sync.sync.webhooks import WebhookManager, service_of
from tanss_sync.tanss.models import TanssEventRule

FREMD = "https://api.tanssx.de/api/v1/sync/appointments/tanss/webhook/"
EIGEN = "https://sync.intern:8843/tanss/"


def rule(rule_id: int, employee_id: int | None, url: str) -> TanssEventRule:
    return TanssEventRule.model_validate({
        "id": rule_id,
        "name": f"Regel {rule_id}",
        "employees": [{"employeeId": employee_id}] if employee_id else [],
        "actions": [{"actionType": "WEBHOOK", "params": {"url": url}}],
    })


class FakeRepo:
    def __init__(self, rules: list[TanssEventRule]) -> None:
        self._rules = rules
        self.deleted: list[int] = []

    def list_event_rules(self) -> list[TanssEventRule]:
        return self._rules

    def delete_event_rule(self, rule_id: int) -> None:
        self.deleted.append(rule_id)


# ------------------------------------------------------------------ Dienst

def test_dienst_ignoriert_die_kennung_am_ende() -> None:
    """Sonst gilt jede Regel als eigenes Ziel und kein Duplikat wird je gefunden."""
    assert service_of(FREMD + "aaaa") == service_of(FREMD + "bbbb")


def test_verschiedene_dienste_bleiben_getrennt() -> None:
    assert service_of(FREMD + "aaaa") != service_of(EIGEN + "aaaa")


# ------------------------------------------------------------------ Duplikate

def test_mehrere_regeln_auf_denselben_dienst_sind_duplikate() -> None:
    repo = FakeRepo([rule(2, 159, FREMD + "a"),
                     rule(11, 159, FREMD + "b"),
                     rule(16, 159, FREMD + "c")])
    report = WebhookManager(repo).inspect()
    assert [r.id for r in report.duplicates] == [11, 16]


def test_die_aelteste_regel_bleibt_stehen() -> None:
    """Das fremde System arbeitet weiter — nur einmal statt dreimal je Vorgang."""
    repo = FakeRepo([rule(20, 1299, FREMD + "a"),
                     rule(3, 1299, FREMD + "b"),
                     rule(15, 1299, FREMD + "c")])
    gruppe = WebhookManager(repo).inspect().groups[0]
    assert 3 not in [r.id for r in gruppe.duplicates]


def test_je_ein_mitarbeiter_ist_kein_duplikat() -> None:
    repo = FakeRepo([rule(2, 159, FREMD + "a"), rule(3, 1299, FREMD + "b")])
    assert WebhookManager(repo).inspect().duplicates == []


def test_regel_ohne_webhook_bleibt_aussen_vor() -> None:
    """Sie macht etwas anderes — etwa eine Mail verschicken."""
    ohne = TanssEventRule.model_validate({
        "id": 5, "employees": [{"employeeId": 159}],
        "actions": [{"actionType": "MAIL", "params": {}}]})
    report = WebhookManager(FakeRepo([ohne])).inspect()
    assert report.without_webhook == [ohne]
    assert report.own == [] and report.foreign == []


# ------------------------------------------------------------------ Herkunft

def test_fremde_regel_wird_als_fremd_erkannt() -> None:
    repo = FakeRepo([rule(2, 159, FREMD + "a")])
    report = WebhookManager(repo, own_callback_url=EIGEN).inspect()
    assert report.foreign_employees == {159}
    assert report.own == []


def test_eigene_regel_zaehlt_nicht_als_fremd() -> None:
    repo = FakeRepo([rule(2, 159, EIGEN + "a")])
    report = WebhookManager(repo, own_callback_url=EIGEN).inspect()
    assert report.foreign_employees == set()
    assert [r.id for r in report.own] == [2]


def test_ohne_eigene_adresse_gilt_jede_regel_als_fremd() -> None:
    """Im Poll-Betrieb gibt es keine eigene Rückrufadresse — dann ist jede Regel fremd."""
    repo = FakeRepo([rule(2, 159, FREMD + "a")])
    assert WebhookManager(repo).inspect().foreign_employees == {159}


def test_gruppe_erkennt_fremde_herkunft() -> None:
    repo = FakeRepo([rule(2, 159, FREMD + "a"), rule(11, 159, FREMD + "b")])
    gruppe = WebhookManager(repo, own_callback_url=EIGEN).inspect().groups[0]
    assert gruppe.is_foreign


# ------------------------------------------------------------------ fehlende Regeln

def test_fehlende_eigene_regeln_werden_benannt() -> None:
    repo = FakeRepo([rule(2, 159, EIGEN + "a")])
    manager = WebhookManager(repo, own_callback_url=EIGEN)
    assert manager.missing_for([159, 1299], manager.inspect()) == [1299]


def test_ohne_rueckrufadresse_wird_keine_regel_angelegt() -> None:
    """TANSS wüsste nicht, wohin es melden soll."""
    import pytest

    with pytest.raises(ValueError, match="callback_base_url"):
        WebhookManager(FakeRepo([])).create_for(159)
