"""Bestehende Gegenstücke übernehmen, statt sie zu verdoppeln.

Der Anlass ist konkret und am Kundensystem nachgewiesen: Abwesenheiten, die eine frühere
Terminsynchronisation nach Outlook geschrieben hat, tragen in TANSS **keine
Kopplungsdaten**. Für uns sehen sie deshalb aus wie „in TANSS vorhanden, in Outlook
nicht" — und ein erster Lauf legte jeden Urlaubstag ein zweites Mal an.

Dasselbe trifft jeden Termin, dessen Kopplung einmal entfernt wurde: etwa weil er in
TANSS zwischenzeitlich in eine Leistung gewandelt und wieder zurückgewandelt wurde.

Zwei Regeln, nicht eine — weil ein Termin und eine Abwesenheit verschieden genau
bestimmt sind:

* **Termin:** Beginn, Ende und Betreff müssen übereinstimmen. Ein Termin ist auf die
  Minute festgelegt; zwei verschiedene Termine zur selben Minute mit demselben Titel
  gibt es praktisch nicht.
* **Fahrt-Block:** Die angrenzende Terminkante und der Betreff genügen — eine Anfahrt
  endet, wenn der Termin beginnt. Die Dauer ist dabei ausdrücklich kein Merkmal: Sie ist
  genau das, was sich ändert, wenn jemand die Fahrtzeit korrigiert.
* **Abwesenheit:** Kalendertag und Betreff genügen. TANSS führt Abwesenheiten je
  Kalendertag, und die Uhrzeit ist dabei eine Konvention, keine Aussage — welche
  Tagesspanne das schreibende System gewählt hat, ist nicht vorhersagbar. Ein Urlaubstag
  ist ein Tagesfakt.

Beide Regeln brechen bei **Mehrdeutigkeit** ab: Passen zwei Kandidaten gleich gut, wird
nicht übernommen. Eine falsche Übernahme wäre schlimmer als ein Duplikat — sie
verknüpfte zwei verschiedene Termine dauerhaft, und jede spätere Änderung träfe den
falschen. Ein Duplikat sieht man und löscht es; eine Fehlkopplung sieht man nicht.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..domain.appointment import Appointment

_WHITESPACE = re.compile(r"\s+")
# Nur Buchstaben und Ziffern zaehlen. Satzzeichen und Schreibweise sind kein Inhalt.
_NON_CONTENT = re.compile(r"[^0-9a-zäöüß]+", re.IGNORECASE)
# Das Firmensuffix ist Beiwerk des schreibenden Systems, kein Titel. Im Bestand tragen
# es manche Eintraege und manche nicht - es darf deshalb ueber eine Uebernahme nicht
# entscheiden.
_COMPANY_SUFFIX = re.compile(r"\s*\((?:Firma|Company):[^)]*\)\s*$")

# Beginn und Ende muessen auf die Minute passen. TANSS rechnet in Minuten, Graph
# liefert Sekunden - mehr Spielraum braucht es nicht, und mehr waere gefaehrlich.
_TOLERANCE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Adoption:
    candidate: Appointment
    reason: str


def subject_key(value: str) -> str:
    """Betreff auf seinen Inhalt reduziert — ohne Firmensuffix, Satzzeichen, Schreibweise."""
    stripped = _COMPANY_SUFFIX.sub("", value or "")
    return _NON_CONTENT.sub("", _WHITESPACE.sub(" ", stripped)).lower()


def _same_moment(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) < _TOLERANCE_SECONDS


def find_adoption(source: Appointment, candidates: list[Appointment], *,
                  to_local: Callable[[datetime], datetime] | None = None
                  ) -> Adoption | None:
    """Sucht in Outlook ein Gegenstück zu einem noch ungekoppelten TANSS-Termin.

    ``source`` muss bereits in der Outlook-Fassung vorliegen — der Betreffvergleich
    läuft zwar ohne Firmensuffix, alles andere aber gegen den fertigen Stand.

    ``to_local`` rechnet in die Anzeigezeitzone um und entscheidet damit, was
    „derselbe Kalendertag" heißt. Ohne Angabe gilt UTC.
    """
    if source.start is None or source.end is None:
        return None

    wanted = subject_key(source.subject)
    if not wanted:
        return None

    usable = [c for c in candidates
              if c.graph_event_id and c.start is not None
              and subject_key(c.subject) == wanted]
    if not usable:
        return None

    exact = [c for c in usable
             if _same_moment(c.start, source.start) and _same_moment(c.end, source.end)]
    if len(exact) == 1:
        return Adoption(exact[0], "gleicher Beginn, gleiches Ende, gleicher Betreff")
    if exact:
        return None  # mehrdeutig - lieber gar nicht

    if source.key.travel_role != "main":
        # Ein Fahrt-Block ist durch seine **angrenzende Kante** bestimmt, nicht durch
        # seine Dauer: Eine Anfahrt endet, wenn der Termin beginnt, eine Abfahrt
        # beginnt, wenn er endet. Am Kundensystem gilt das ausnahmslos - die Dauern
        # reichen dabei von 12 bis 30 Minuten. Wer auf die Dauer vergliche, legte
        # jeden bestehenden Block ein zweites Mal an; eine geaenderte Fahrtzeit ist
        # eine Aenderung am selben Block, kein anderer Block.
        anchor = "end" if source.key.travel_role == "travel_to" else "start"
        touching = [c for c in usable
                    if _same_moment(getattr(c, anchor), getattr(source, anchor))]
        if len(touching) != 1:
            return None
        return Adoption(
            touching[0],
            "bestehender Fahrt-Block an derselben Terminkante — übernommen, "
            "die Fahrtzeit selbst wird angeglichen")

    if not source.kind.is_absence:
        return None

    local = to_local or (lambda value: value)
    day = local(source.start).date()
    same_day = [c for c in usable if local(c.start).date() == day]
    if len(same_day) != 1:
        return None

    return Adoption(
        same_day[0],
        "Abwesenheit am selben Kalendertag mit gleichem Betreff — die Tagesspanne "
        "des schreibenden Systems weicht ab, der Tag ist dieselbe Abwesenheit",
    )
