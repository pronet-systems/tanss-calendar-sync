"""Änderungserkennung.

**Jede Seite wird mit ihrem eigenen vorherigen Zustand verglichen — nie mit der
anderen Seite.**

Das ist der Kern und der einzige Weg, der aufgeht. Ein Termin sieht in TANSS und in
Outlook zwangsläufig verschieden aus, ohne dass sich inhaltlich etwas geändert hätte:
TANSS führt Klartext, Graph führt HTML. Die Rückwandlung liefert Markdown — eine
Trennlinie wird zu ``* * *``, ``1.`` wird als ``1\\.`` maskiert. Der Betreff trägt in
Outlook das ``(Firma: …)``-Suffix, in TANSS nicht. Der Ort ist in TANSS meist leer.

Diese Darstellungen gleichen sich **niemals** an. Wer sie gegeneinander hält, hält jeden
Termin bei jedem Lauf für geändert, schreibt ihn neu, findet beim nächsten Lauf wieder
einen Unterschied — und läuft endlos. Jede Normalisierung dagegen ist ein Pflaster, das
irgendwann eine echte Änderung verschluckt.

Deshalb: Für jede Seite wird eine Prüfsumme über ihre eigenen Felder gebildet und mit der
Prüfsumme vom letzten Abgleich verglichen. Weicht sie ab, hat sich **dort** etwas
geändert. Die Gegenseite kommt dabei gar nicht vor.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from ..domain.appointment import Appointment
from ..state.records import LinkRecord

_WHITESPACE = re.compile(r"\s+")
_COMPANY_SUFFIX = re.compile(r"\s*\((?:Firma|Company):[^)]*\)\s*$")

# Zeitliche Toleranz: TANSS rechnet in Minuten, Graph liefert Sekunden.
_TIME_TOLERANCE_SECONDS = 60

# Felder, die in die Pruefsumme einer Seite eingehen. Alles, was hier steht, loest
# bei Aenderung einen Abgleich aus.
_FINGERPRINT_FIELDS = ("subject", "body", "location", "start", "end",
                       "all_day", "show_as")


class Changed(StrEnum):
    NEITHER = "neither"
    TANSS = "tanss"
    GRAPH = "graph"
    BOTH = "both"
    UNKNOWN = "unknown"      # keine Ausgangsmarke vorhanden


@dataclass(frozen=True, slots=True)
class ChangeVerdict:
    side: Changed
    tanss_hash: str
    graph_hash: str

    @property
    def is_conflict(self) -> bool:
        return self.side is Changed.BOTH


def fingerprint(appointment: Appointment) -> str:
    """Prüfsumme über den Zustand **einer** Seite.

    Wird nur mit der Prüfsumme derselben Seite vom letzten Abgleich verglichen, nie mit
    der der Gegenseite. Deshalb darf sie unnormalisiert über die Rohwerte laufen —
    Formatunterschiede zwischen den Systemen spielen hier keine Rolle mehr.
    """
    payload = {}
    for name in _FINGERPRINT_FIELDS:
        value = getattr(appointment, name)
        if hasattr(value, "isoformat"):
            # Auf die Minute genau - Sekundenbruchteile sind kein Inhalt.
            payload[name] = value.replace(second=0, microsecond=0).isoformat()
        else:
            payload[name] = str(value)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def what_changed(tanss: Appointment | None, graph: Appointment | None,
                 link: LinkRecord | None) -> ChangeVerdict:
    """Wo hat sich seit dem letzten Abgleich etwas getan?

    Ohne gespeicherte Ausgangsmarke lautet die Antwort ``UNKNOWN``. Dann wird **nicht**
    geschrieben, sondern der aktuelle Stand als Ausgangspunkt übernommen — sonst
    schriebe der erste Lauf jeden Bestandstermin einmal neu, ohne dass sich irgendwo
    etwas geändert hätte.
    """
    t_hash = fingerprint(tanss) if tanss else ""
    g_hash = fingerprint(graph) if graph else ""

    if link is None or (not link.last_hash_tanss and not link.last_hash_graph):
        return ChangeVerdict(Changed.UNKNOWN, t_hash, g_hash)

    t_changed = bool(tanss) and t_hash != (link.last_hash_tanss or "")
    g_changed = bool(graph) and g_hash != (link.last_hash_graph or "")

    if t_changed and g_changed:
        side = Changed.BOTH
    elif t_changed:
        side = Changed.TANSS
    elif g_changed:
        side = Changed.GRAPH
    else:
        side = Changed.NEITHER
    return ChangeVerdict(side, t_hash, g_hash)


# --------------------------------------------------------------------------- Felder

def fields_to_write(source: Appointment, target: Appointment) -> set[str]:
    """Welche Felder ein Update übertragen muss.

    Wird erst aufgerufen, **nachdem** feststeht, dass sich die Quelle geändert hat.
    Hier geht es nur noch darum, ein möglichst kleines ``PATCH`` zu bauen — nicht mehr
    um die Frage, *ob* etwas zu tun ist. Ein zu großzügiger Vergleich schadet hier
    nicht: Er überträgt höchstens ein Feld zu viel.
    """
    changed: set[str] = set()

    if _plain(_COMPANY_SUFFIX.sub("", source.subject)) != \
       _plain(_COMPANY_SUFFIX.sub("", target.subject)):
        changed.add("subject")

    if _plain(source.body) != _plain(target.body):
        changed.add("body")

    if source.location.strip() and _plain(source.location) != _plain(target.location):
        changed.add("location")

    for name in ("start", "end"):
        a, b = getattr(source, name), getattr(target, name)
        if a is None or b is None:
            if a is not b:
                changed.add(name)
        elif abs((a - b).total_seconds()) >= _TIME_TOLERANCE_SECONDS:
            changed.add(name)

    if source.all_day != target.all_day:
        changed.add("all_day")
    if source.show_as != target.show_as:
        changed.add("show_as")

    return changed


def _plain(value: str | None) -> str:
    return _WHITESPACE.sub(" ", value or "").strip()
