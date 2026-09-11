"""Eine Datei ersetzen, ohne ihre Rechte zu verlieren.

Konfiguration und Token werden nicht überschrieben, sondern daneben neu geschrieben
und umbenannt. Das ist richtig so: Ein abgebrochener Schreibvorgang hinterlässt dann
keine halbe Konfiguration und kein halbes Token.

Der Preis ist, dass die neue Datei dem schreibenden Prozess gehört. Läuft der als
root — ``sudo tanss-sync users enable`` —, so gehört sie danach root allein, und der
Dienstbenutzer ist von seiner eigenen Konfiguration ausgesperrt. Er stirbt beim
nächsten Start mit ``PermissionError``, und zwar erst dann: Der Befehl selbst meldet
Erfolg.

Rechte und Eigentümer werden deshalb **auf dem offenen Dateideskriptor** gesetzt,
bevor umbenannt wird. Der Weg über den Pfad wäre angreifbar: ``os.chmod`` und
``os.chown`` folgen Symlinks, und zwischen dem Umbenennen und dem Rechtesetzen liegt
ein Fenster, in dem jeder, der im Verzeichnis umbenennen darf, den Namen erneut
ersetzen kann — seit das Konfigurationsverzeichnis gruppenschreibbar ist, ist das der
Dienstbenutzer selbst. Ein Deskriptor lässt sich nicht unterschieben, und weil
``os.replace`` atomar ist, erscheint die Datei sofort mit ihren endgültigen Rechten.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

#: Niemals an andere vergeben, auch nicht, wenn die Vorgängerdatei es tat. Eine
#: Konfiguration mit Zugangsdaten und ein Token gehen niemanden sonst etwas an, und
#: geschrieben wird ohnehin nie in die Datei, sondern immer neben sie.
_NIE_VERGEBEN = 0o027


def rechte_vorher(pfad: Path) -> os.stat_result | None:
    """Der Zustand vor dem Ersetzen — ``None``, wenn es nichts zu erben gibt.

    ``follow_symlinks=False``, damit nicht die Rechte eines fremden Ziels einwandern.
    Ein Symlink selbst zählt ebenfalls nicht: Er trägt immer ``lrwxrwxrwx``, und wo
    eine Konfiguration oder ein Token erwartet wird, ist er ohnehin nichts, dessen
    Rechte man fortschreiben möchte. Dann gilt der strenge Standard.
    """
    try:
        zustand = os.stat(pfad, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return None if stat.S_ISLNK(zustand.st_mode) else zustand


def _modus_fuer(vorher: os.stat_result | None, standard: int) -> int:
    if vorher is None:
        return standard
    # Die vorgefundenen Rechte gelten — aber nur nach unten. Eine Datei, die jemand
    # versehentlich auf 0644 gesetzt hat, würde sonst über jede Erneuerung hinweg
    # offen bleiben; vorher zog ein festes chmod sie jedes Mal wieder zurück.
    return stat.S_IMODE(vorher.st_mode) & ~_NIE_VERGEBEN


def atomar_ersetzen(pfad: Path, schreiber: Callable[[object], None], *,
                    standard_mode: int) -> None:
    """``pfad`` durch das ersetzen, was ``schreiber`` in die offene Datei schreibt.

    ``schreiber`` bekommt ein zum Schreiben geöffnetes Textobjekt. Rechte und
    Eigentümer der bisherigen Datei gelten für die neue; gab es keine, gilt
    ``standard_mode``.
    """
    pfad.parent.mkdir(parents=True, exist_ok=True)
    vorher = rechte_vorher(pfad)
    modus = _modus_fuer(vorher, standard_mode)

    fd, tmp = tempfile.mkstemp(dir=str(pfad.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            schreiber(handle)
            handle.flush()
            os.fsync(handle.fileno())
            # Erst die Rechte, dann umbenennen — siehe Modulkopf.
            os.fchmod(handle.fileno(), modus)
            if vorher is not None and os.geteuid() == 0:
                os.fchown(handle.fileno(), vorher.st_uid, vorher.st_gid)
        os.replace(tmp, pfad)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def verzeichnis_anlegen_wie(verzeichnis: Path, vorbild: Path, *, modus: int) -> None:
    """Ein Laufzeitverzeichnis anlegen, das dem Dienstbenutzer gehört.

    ``/run`` gehört root, und wer dort als root ein Verzeichnis anlegt, hinterlässt es
    root. Der Dienst kann seine Sperrdatei dann nicht mehr schreiben. Welcher Benutzer
    gemeint ist, verrät das ``vorbild``: Zustandsdatenbank und Sperrdatei sind
    Laufzeitdaten desselben Dienstes.

    Den Eigentümer setzt das nur unterhalb von ``/run``. Pfad und Vorbild stammen beide
    aus der Konfiguration, und die schreibt der Dienstbenutzer selbst — ohne diese
    Schranke ließe sich root damit an beliebiger Stelle ein fremdeigenes Verzeichnis
    entlocken.
    """
    if verzeichnis.exists():
        return
    verzeichnis.mkdir(parents=True, exist_ok=True)
    os.chmod(verzeichnis, modus)
    if os.geteuid() != 0 or not _unterhalb_von_run(verzeichnis):
        return
    for kandidat in (vorbild, vorbild.parent):
        eigner = rechte_vorher(kandidat)
        if eigner is not None and eigner.st_uid != 0:
            os.chown(verzeichnis, eigner.st_uid, eigner.st_gid)
            return


def _unterhalb_von_run(pfad: Path) -> bool:
    return Path("/run") in pfad.resolve().parents
