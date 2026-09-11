"""Kanonisierung und Vergleich von Termin-UIDs.

Die einzige Stelle im Projekt, an der UIDs kanonisiert und verglichen werden.
Verstreut man das über Mapper und Reconciler, driftet es auseinander — und jede
Abweichung erzeugt bei jedem Lauf ein Duplikat.

Hintergrund
-----------
Microsoft Graph liefert ``iCalUId`` bei Terminen, die in Outlook entstanden sind, als
hexadezimale Global-Object-ID (``040000008200E00074C5B7101A82E008...``). Stammt der
Termin aus einem Fremdsystem, bettet Outlook dessen ursprüngliche iCalendar-UID in
diesen Block ein — hinter der ASCII-Marke ``vCal-Uid``. TANSS speichert in
``metaInfos.SYNC_GROUP`` die *eingebettete* UID, nicht die Hülle.

Wer das übersieht, findet für jede externe Einladung keinen Partner und legt Duplikate an.
"""

from __future__ import annotations

import binascii

# Marke, hinter der Outlook die urspruengliche iCalendar-UID einbettet.
# Aufbau: b"vCal-Uid" + 4 Byte Laengen-/Versionsfeld + nullterminierte ASCII-UID.
_VCAL_MARKER = b"vCal-Uid"
_MARKER_SKIP = len(_VCAL_MARKER) + 4

# Vergleichsregel von TANSS: unterhalb dieser Laenge gilt strikte Gleichheit.
_STRICT_BELOW = 100
_PREFIX_LEN = 32
_SUFFIX_LEN = 70

_SEQ_SEPARATOR = "|"
NO_SEQUENCE = -1

# Ersatzschluessel fuer einen TANSS-Termin, der noch nie nach Outlook gekoppelt wurde.
# Die echte UID entsteht erst beim Anlegen in Graph - bis dahin braucht jeder Termin
# trotzdem einen eigenen, eindeutigen Schluessel, sonst fielen alle ungekoppelten
# Termine zu einem einzigen Paar zusammen.
PENDING_PREFIX = "pending:"


def is_pending(uid: str | None) -> bool:
    """Ob dieser Schlüssel ein Platzhalter ist — der Termin also noch nie gekoppelt war."""
    return bool(uid) and uid.startswith(PENDING_PREFIX)


def pending_uid(support_id: int) -> str:
    return f"{PENDING_PREFIX}{support_id}"


# Ersatzschluessel fuer einen Outlook-Termin ohne iCalUId. Er ist an die Termin-Kennung
# gebunden und damit eindeutig und stabil. Eine TANSS-SYNC_GROUP sieht nie so aus, eine
# Fehlpaarung ist damit ausgeschlossen.
ORPHAN_PREFIX = "graph:"


def orphan_uid(event_id: str) -> str:
    return f"{ORPHAN_PREFIX}{event_id}"


def is_orphan(uid: str | None) -> bool:
    return bool(uid) and uid.startswith(ORPHAN_PREFIX)


def occurrence_sequence(start) -> int:
    """Kennzeichnet eine Serien-Occurrence über **ihren Zeitpunkt**.

    TANSS nummeriert Occurrences mit ``recurrenceRuleSequenceId``, Graph kennt keine
    solche Nummer — jede Occurrence trägt dort nur ihre eigene Termin-Kennung. Über die
    Systemgrenze hinweg bleibt als gemeinsames Merkmal allein der Beginn: Zwei
    Occurrences derselben Serie zur selben Minute sind derselbe Termin.

    Die TANSS-eigene Nummer bleibt davon unberührt — sie steht weiter am Termin und wird
    verwendet, wo TANSS sie erwartet. Sie taugt nur nicht als gemeinsamer Schlüssel.

    Gezählt wird in vollen Minuten seit der Epoche: TANSS rechnet ohnehin in Minuten,
    und Sekundenbruchteile aus Graph dürfen kein zweites Paar aufmachen.
    """
    return int(start.timestamp()) // 60


def canonical_uid(ical_uid: str | None) -> str:
    """Liefert die kanonische UID zu einer Graph-``iCalUId``.

    Enthaelt der hexadezimal dekodierte Block die Marke ``vCal-Uid``, ist die dahinter
    stehende nullterminierte ASCII-Kette die UID. Andernfalls gilt die ``iCalUId`` selbst.

    Die Funktion ist absichtlich tolerant: Alles, was sich nicht als Hex dekodieren
    laesst, wird unveraendert zurueckgegeben. Ein unbrauchbarer Wert waere hier ein
    schlechterer Ausgang als ein unveraenderter.
    """
    if not ical_uid:
        return ""

    raw = ical_uid.strip()
    embedded = _extract_embedded_uid(raw)
    return embedded if embedded else raw


def _extract_embedded_uid(value: str) -> str | None:
    """Sucht die eingebettete vCal-Uid. ``None``, wenn keine vorhanden ist."""
    # Nur reine Hex-Ketten gerader Laenge koennen eine Global-Object-ID sein.
    if len(value) < 2 * _MARKER_SKIP or len(value) % 2 != 0:
        return None
    try:
        decoded = binascii.unhexlify(value)
    except (binascii.Error, ValueError):
        return None

    marker = decoded.find(_VCAL_MARKER)
    if marker < 0:
        return None

    payload = decoded[marker + _MARKER_SKIP :]
    # Die eingebettete UID ist nullterminiert; danach folgt Fuellmaterial.
    end = payload.find(b"\x00")
    if end >= 0:
        payload = payload[:end]

    try:
        embedded = payload.decode("ascii").strip()
    except UnicodeDecodeError:
        return None

    return embedded or None


def uid_matches(a: str | None, b: str | None) -> bool:
    """Vergleicht zwei UIDs nach der Regel, die TANSS serverseitig anwendet.

    Ist *eine* der beiden Ketten kuerzer als 100 Zeichen, gilt strikte Gleichheit.
    Sind *beide* mindestens 100 Zeichen lang, werden nur die ersten 32 und die letzten
    70 Zeichen verglichen — der Mittelteil wird ignoriert.

    Ein schlichtes ``==`` wuerde Bestandskopplungen nicht finden und bei jedem Lauf
    Duplikate anlegen.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < _STRICT_BELOW or len(b) < _STRICT_BELOW:
        return False
    return a[:_PREFIX_LEN] == b[:_PREFIX_LEN] and a[-_SUFFIX_LEN:] == b[-_SUFFIX_LEN:]


def parse_sync_group(value: str | None) -> tuple[str, int]:
    """Zerlegt einen ``SYNC_GROUP``-Wert in ``(uid, sequence)``.

    Ohne Suffix ist die Sequenz ``-1``. Ein Suffix ``|0`` ist gueltig und bedeutet
    Occurrence Nummer 0 — der Wert darf nicht per Falsy-Pruefung verschluckt werden.

    Achtung: Beim *Lesen* traegt ``SYNC_GROUP`` in aller Regel **keine** Sequenz —
    expandierte Occurrences erben die Metadaten des Masters unveraendert. Massgeblich
    fuer die Identitaet einer Occurrence ist ``recurrenceRuleSequenceId`` am Support.
    Die Sequenz aus ``SYNC_GROUP`` ist nur Zusatzquelle.
    """
    if not value:
        return "", NO_SEQUENCE
    uid, sep, seq = value.rpartition(_SEQ_SEPARATOR)
    if not sep:
        return value, NO_SEQUENCE
    try:
        return uid, int(seq)
    except ValueError:
        # Ein Trennzeichen, das nicht zu einer Sequenz gehoert (kommt in UIDs
        # aus Fremdsystemen vor) - dann ist der ganze Wert die UID.
        return value, NO_SEQUENCE


def format_sync_group(uid: str, sequence: int = NO_SEQUENCE) -> str:
    """Baut einen ``SYNC_GROUP``-Wert. Sequenz < 0 ergibt nur die UID."""
    if sequence < 0:
        return uid
    return f"{uid}{_SEQ_SEPARATOR}{sequence}"
