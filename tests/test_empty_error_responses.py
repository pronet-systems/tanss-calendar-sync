"""Eine leere Antwort mit Fehlerstatus ist kein leerer Erfolg.

Das ist der gefährlichste Fehler, den dieses Werkzeug machen kann, und er sieht
harmlos aus. Beide HTTP-Schichten hatten am Anfang der Auswertung eine Abkürzung:

    if status == 204 or not response.content:
        return {}

Sie stand **vor** der Statusprüfung. Eine leere ``401`` vom Tokendienst, eine leere
``403`` nach entzogenem Postfachzugriff, eine leere ``502`` von einem Zwischenknoten —
alle drei kamen als leerer Erfolg zurück. Die Folge reicht weiter, als es zunächst
aussieht:

* ``list_appointments`` liefert eine leere Liste → jeder gekoppelte Termin gilt als
  verschwunden.
* ``get_support`` liefert ``None`` → und genau dieses ``None`` ist im Löschpfad der
  **Nachweis**, dass ein Termin entfernt wurde.

Ein abgelaufenes Token wäre damit zum Löschbefehl für den gesamten Bestand geworden.
Die Mengenbremse hätte das vielleicht abgefangen — aber die ist eine Zusatzsicherung
und steht ab Werk aus. Tragen muss der Nachweis selbst.
"""

from __future__ import annotations

import httpx
import pytest

from tanss_sync.microsoft.client import GraphClient, GraphError, GraphNotFound
from tanss_sync.tanss.client import TANSS_X_PREFIX, TanssClient
from tanss_sync.tanss.errors import TanssAuthError, TanssError, TanssNotFound
from tanss_sync.tanss.repository import TanssRepository

FEHLERHAFT = [401, 403, 500, 502, 504]


class Zugang:
    def header(self) -> dict:
        return {"apiToken": "Bearer x"}

    def token(self) -> str:
        return "x"


def tanss_client(status: int, body: bytes = b"") -> TanssClient:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, content=body))
    return TanssClient("https://tanss.example", Zugang(),
                       client=httpx.Client(transport=transport))


def graph_client(status: int, body: bytes = b"") -> GraphClient:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, content=body))
    return GraphClient(Zugang(), client=httpx.Client(transport=transport))


# ------------------------------------------------------------------ TANSS

@pytest.mark.parametrize("status", FEHLERHAFT)
def test_leerer_fehler_wird_nicht_als_erfolg_gelesen(status: int) -> None:
    client = tanss_client(status)
    with pytest.raises(TanssError):
        client.get(f"{TANSS_X_PREFIX}/supports/4711")
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_leere_abweisung_meldet_ein_zugangsproblem(status: int) -> None:
    client = tanss_client(status)
    with pytest.raises(TanssAuthError):
        client.get(f"{TANSS_X_PREFIX}/technicians")
    client.close()


def test_leere_404_bleibt_ein_echtes_nicht_gefunden() -> None:
    """Das ist der **einzige** Weg, auf dem ein Termin als gelöscht gelten darf."""
    client = tanss_client(404)
    with pytest.raises(TanssNotFound):
        client.get(f"{TANSS_X_PREFIX}/supports/4711")
    client.close()


@pytest.mark.parametrize("status", [200, 204])
def test_leerer_erfolg_bleibt_ein_leerer_erfolg(status: int) -> None:
    client = tanss_client(status)
    assert client.get(f"{TANSS_X_PREFIX}/supports/4711") == {}
    client.close()


# --------------------------------------------------- der Löschnachweis selbst

@pytest.mark.parametrize("status", FEHLERHAFT)
def test_kaputte_verbindung_ist_kein_loeschnachweis(status: int) -> None:
    """``get_support`` darf ``None`` nur bei einer echten 404 liefern.

    Jedes andere ``None`` wäre ein Löschbefehl für einen Termin, der noch existiert.
    """
    repo = TanssRepository(tanss_client(status))
    with pytest.raises(TanssError):
        repo.get_support(4711)


def test_echte_404_liefert_none() -> None:
    repo = TanssRepository(tanss_client(404))
    assert repo.get_support(4711) is None


def test_leerer_inhalt_mit_erfolgsstatus_ist_kein_loeschnachweis() -> None:
    """Eine 200 ohne verwertbaren Inhalt heißt „etwas lief schief", nicht „ist weg"."""
    repo = TanssRepository(tanss_client(200))
    with pytest.raises(TanssError, match="kein Löschnachweis"):
        repo.get_support(4711)


# ------------------------------------------------------------------ Graph

@pytest.mark.parametrize("status", [401, 403])
def test_graph_leerer_fehler_wird_nicht_als_erfolg_gelesen(status: int) -> None:
    client = graph_client(status)
    with pytest.raises(GraphError):
        client.get("/users/x/events/y")
    client.close()


def test_graph_leere_404_bleibt_ein_echtes_nicht_gefunden() -> None:
    client = graph_client(404)
    with pytest.raises(GraphNotFound):
        client.get("/users/x/events/y")
    client.close()


@pytest.mark.parametrize("status", [200, 204])
def test_graph_leerer_erfolg_bleibt_ein_leerer_erfolg(status: int) -> None:
    client = graph_client(status)
    assert client.get("/users/x/events/y") == {}
    client.close()
