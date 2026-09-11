"""Ein schreibender Befehl als root sperrt den Dienst nicht aus.

Am Produktivsystem gleich zweimal aufgetreten, beide Male mit demselben Muster:

* ``sudo tanss-sync users enable 159`` schrieb ``config.json`` neu. Die Datei
  entsteht dabei als Kopie — ``mkstemp`` daneben, dann ``os.replace`` —, gehörte
  danach ``root:root`` mit ``0600``, und der Dienstbenutzer war von seiner eigenen
  Konfiguration ausgesperrt. Der Dienst starb beim nächsten Start mit
  ``PermissionError: /etc/tanss-calendar-sync/config.json``.
* Derselbe Befehl legte ``/run/tanss-calendar-sync`` für seine Sperrdatei an — als
  ``root:root``. Auch daran starb der Dienst, mit ``PermissionError`` auf die
  Sperrdatei.

Beide Male musste jemand die Rechte von Hand zurechtrücken, und beide Male fiel es
erst auf, als der Dienst schon nicht mehr lief. Wer eine Datei ersetzt, übernimmt
die Rechte dessen, was er ersetzt.
"""

from __future__ import annotations

import json
import os
import stat

from tanss_sync.config.models import AppConfig
from tanss_sync.config.store import ConfigStore
from tanss_sync.config.secrets import SecretRef

KONFIG = {
    "version": 1,
    "tanss": {"base_url": "https://example.invalid/backend",
              "token_ref": "file:/tmp/kein-token", "token_owner_employee_id": 1},
    "microsoft": {"tenant_id": "t", "client_id": "c",
                  "auth": {"mode": "secret", "client_secret_ref": "file:/tmp/kein-secret"}},
    "users": [],
}


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_konfiguration_behaelt_ihre_rechte(tmp_path) -> None:
    ziel = tmp_path / "config.json"
    ziel.write_text(json.dumps(KONFIG), encoding="utf-8")
    os.chmod(ziel, 0o640)          # root:tanss-sync 0640, wie install.sh es anlegt

    store = ConfigStore(ziel)
    store.save(store.load())

    assert _mode(ziel) == 0o640, (
        "0600 sperrt den Dienstbenutzer aus — die vorgefundenen Rechte gelten")


def test_neue_konfiguration_entsteht_eng(tmp_path) -> None:
    """Ohne Vorgänger gibt es nichts zu übernehmen: dann das strengste Maß."""
    ziel = tmp_path / "neu" / "config.json"
    ConfigStore(ziel).save(AppConfig.model_validate(KONFIG))
    assert _mode(ziel) == 0o600


def test_token_behaelt_seine_rechte(tmp_path) -> None:
    ziel = tmp_path / "token"
    ziel.write_text("Bearer alt\n", encoding="utf-8")
    os.chmod(ziel, 0o640)

    SecretRef(f"file:{ziel}").write("Bearer neu")

    assert ziel.read_text(encoding="utf-8").strip() == "Bearer neu"
    assert _mode(ziel) == 0o640, "sonst kann der Dienst sein eigenes Token nicht lesen"


def test_neues_token_entsteht_eng(tmp_path) -> None:
    ziel = tmp_path / "neu" / "token"
    SecretRef(f"file:{ziel}").write("Bearer neu")
    assert _mode(ziel) == 0o600


def test_laufzeitverzeichnis_folgt_dem_dienstbenutzer(tmp_path) -> None:
    """``/run`` gehört root — wer dort als root anlegt, sperrt den Dienst aus."""
    from tanss_sync.util.dateien import verzeichnis_anlegen_wie

    vorbild = tmp_path / "var" / "state.db"
    vorbild.parent.mkdir(parents=True)
    vorbild.write_text("", encoding="utf-8")

    ziel = tmp_path / "run" / "tanss-calendar-sync"
    verzeichnis_anlegen_wie(ziel, vorbild, modus=0o770)

    assert ziel.is_dir()
    assert _mode(ziel) == 0o770, "der Dienstbenutzer muss die Sperrdatei schreiben können"


def test_vorhandenes_laufzeitverzeichnis_bleibt_unberuehrt(tmp_path) -> None:
    """systemd legt es per ``RuntimeDirectory=`` an — daran wird nicht gedreht."""
    from tanss_sync.util.dateien import verzeichnis_anlegen_wie

    ziel = tmp_path / "run"
    ziel.mkdir()
    os.chmod(ziel, 0o750)
    verzeichnis_anlegen_wie(ziel, tmp_path / "fehlt.db", modus=0o770)
    assert _mode(ziel) == 0o750


def test_sperre_legt_ihr_verzeichnis_an(tmp_path) -> None:
    from tanss_sync.util.lock import ProcessLock

    db = tmp_path / "var" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_text("", encoding="utf-8")

    sperre = tmp_path / "run" / "dienst" / "lock"
    with ProcessLock(sperre, "test", eigentuemer_wie=db):
        assert sperre.exists()
    assert not sperre.exists(), "die Sperre wird wieder freigegeben"


# ------------------------------------------------------ Untergrenze und Symlinks

def test_zu_weite_rechte_heilen_aus(tmp_path) -> None:
    """Die vorgefundenen Rechte gelten — aber nur nach unten.

    Ein mit ``tee`` und Umask 022 angelegtes Token ist 0644. Vorher zog ein festes
    ``chmod`` es bei jeder Erneuerung auf 0600 zurück; würden die Rechte blind
    übernommen, bliebe es dauerhaft weltlesbar.
    """
    ziel = tmp_path / "token"
    ziel.write_text("Bearer alt\n", encoding="utf-8")
    os.chmod(ziel, 0o644)

    SecretRef(f"file:{ziel}").write("Bearer neu")

    assert _mode(ziel) == 0o640, "fremde Benutzer dürfen das Token nicht lesen"


def test_gruppenschreibrecht_wird_nicht_uebernommen(tmp_path) -> None:
    ziel = tmp_path / "config.json"
    ziel.write_text(json.dumps(KONFIG), encoding="utf-8")
    os.chmod(ziel, 0o666)

    store = ConfigStore(ziel)
    store.save(store.load())

    assert _mode(ziel) == 0o640


def test_sicherung_des_tokens_ist_genauso_geschuetzt(tmp_path) -> None:
    """Die ``.bak``-Kopie enthält dasselbe Geheimnis und braucht dieselben Rechte."""
    ziel = tmp_path / "token"
    ziel.write_text("Bearer alt\n", encoding="utf-8")
    os.chmod(ziel, 0o600)

    SecretRef(f"file:{ziel}").write("Bearer neu")

    sicherung = tmp_path / "token.bak"
    assert sicherung.read_text(encoding="utf-8").strip() == "Bearer alt"
    assert _mode(sicherung) & 0o077 == 0, "sonst liegt das alte Token offen daneben"


def test_ein_untergeschobener_symlink_erbt_nichts(tmp_path) -> None:
    """``os.stat`` folgt Symlinks — die Rechte des Ziels sind nicht die der Datei."""
    offen = tmp_path / "offen"
    offen.write_text("egal", encoding="utf-8")
    os.chmod(offen, 0o666)

    ziel = tmp_path / "token"
    ziel.symlink_to(offen)

    SecretRef(f"file:{ziel}").write("Bearer neu")

    assert not ziel.is_symlink(), "der Link wird ersetzt, nicht durch ihn hindurch geschrieben"
    assert _mode(ziel) == 0o600, "ein Symlink stiftet keine Rechte"
    assert offen.read_text(encoding="utf-8") == "egal", "das Ziel bleibt unberührt"


def test_eigentuemer_wird_als_root_uebernommen(tmp_path, monkeypatch) -> None:
    """Der Kern der Sache — und nur als root erreichbar, deshalb gestellt.

    Gesetzt wird auf dem Dateideskriptor, nicht auf dem Pfad: Ein ``os.chown`` auf den
    Pfad folgte einem Symlink, den jemand zwischen Umbenennen und Rechtesetzen
    unterschiebt.
    """
    from tanss_sync.util import dateien

    gerufen: list[tuple] = []
    monkeypatch.setattr(dateien.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dateien.os, "fchown",
                        lambda fd, uid, gid: gerufen.append((fd, uid, gid)))

    ziel = tmp_path / "config.json"
    ziel.write_text(json.dumps(KONFIG), encoding="utf-8")
    vorher = os.stat(ziel)

    store = ConfigStore(ziel)
    store.save(store.load())

    assert len(gerufen) == 1, "genau einmal, und zwar auf dem Deskriptor"
    fd, uid, gid = gerufen[0]
    assert isinstance(fd, int) and fd > 2, "ein Deskriptor, kein Pfad"
    assert (uid, gid) == (vorher.st_uid, vorher.st_gid)


def test_ohne_vorgaenger_wird_kein_eigentuemer_gesetzt(tmp_path, monkeypatch) -> None:
    from tanss_sync.util import dateien

    gerufen: list[tuple] = []
    monkeypatch.setattr(dateien.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dateien.os, "fchown",
                        lambda fd, uid, gid: gerufen.append((fd, uid, gid)))

    ConfigStore(tmp_path / "neu" / "config.json").save(AppConfig.model_validate(KONFIG))
    assert gerufen == [], "es gibt niemanden, dessen Eigentum zu erhalten wäre"
