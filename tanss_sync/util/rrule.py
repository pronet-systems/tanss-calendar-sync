"""Zwischen RFC-5545-Wiederholungsregel und dem Graph-``recurrence``-Objekt.

TANSS führt eine Regelzeichenkette (``FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261027T141500Z``),
Graph ein strukturiertes Objekt aus ``pattern`` und ``range``. Beide beschreiben dasselbe,
aber nicht deckungsgleich — drei Stellen entscheiden darüber, ob eine Serie nach der
Übertragung noch auf denselben Tagen liegt:

**Endlose Serien.** Graph erlaubt ``range.type = noEnd``, TANSS lehnt sie ab
(``INFINITE_RECURRING_EVENTS_MUST_HAVE_END_DATE``). Beim Weg nach TANSS bekommt eine
endlose Serie deshalb ein Enddatum, das bei jedem Lauf nachgezogen wird — sie wirkt
dadurch endlos, ohne es zu sein. Die Spanne steht in ``sync.infinite_series_end_years``.

**Zeitzone des Abschlusses.** TANSS speichert ``UNTIL`` als UTC-Zeitpunkt, Graph führt in
``range.endDate`` ein reines Kalenderdatum samt ``recurrenceTimeZone``. Wer den UTC-Zeitpunkt
unbesehen als Datum übernimmt, verliert bei einer Serie, die abends nach 22 Uhr Ortszeit
endet, den letzten Termin — und gewinnt bei einer frühen einen zusätzlichen. Umgerechnet
wird deshalb immer über die Anzeigezeitzone.

**Der Wochentag ist die Aussage, nicht das Datum.** Eine wöchentliche Serie ohne ``BYDAY``
ist in RFC 5545 durch ihren Startwochentag bestimmt; Graph verlangt ``daysOfWeek``
ausdrücklich. Fehlt die Angabe, wird sie aus dem Startdatum abgeleitet, statt einen
Vorgabewert zu setzen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_DAYS_TO_GRAPH = {
    "MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday",
    "FR": "friday", "SA": "saturday", "SU": "sunday",
}
_DAYS_TO_RRULE = {v: k for k, v in _DAYS_TO_GRAPH.items()}
_WEEKDAY_BY_INDEX = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

_FREQ_TO_GRAPH = {
    "DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "absoluteMonthly",
    "YEARLY": "absoluteYearly",
}


class UnsupportedRecurrence(ValueError):
    """Eine Regel, die sich nicht verlustfrei übertragen lässt.

    Bewusst ein Fehler und kein stiller Rückfall: Eine halb übertragene Serie liegt auf
    anderen Tagen als das Original, und das fällt erst auf, wenn jemand zum falschen
    Termin erscheint.
    """


@dataclass(frozen=True, slots=True)
class Rule:
    """Die gemeinsame Zwischenform."""

    freq: str
    interval: int = 1
    by_day: tuple[str, ...] = ()
    by_month_day: int | None = None
    by_month: int | None = None
    count: int | None = None
    until: datetime | None = None


# --------------------------------------------------------------------------- lesen

def parse_rrule(value: str) -> Rule:
    """Zerlegt eine RFC-5545-Regel. ``RRULE:``-Präfix ist erlaubt."""
    text = (value or "").strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    if not text:
        raise UnsupportedRecurrence("leere Wiederholungsregel")

    parts: dict[str, str] = {}
    for chunk in text.split(";"):
        if not chunk:
            continue
        name, _, raw = chunk.partition("=")
        parts[name.strip().upper()] = raw.strip()

    freq = parts.get("FREQ", "").upper()
    if freq not in _FREQ_TO_GRAPH:
        raise UnsupportedRecurrence(f"Frequenz {freq or '(fehlt)'} wird nicht unterstützt")

    until = _parse_until(parts["UNTIL"]) if "UNTIL" in parts else None
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None

    by_day = tuple(
        day.upper()[-2:] for day in parts.get("BYDAY", "").split(",") if day.strip()
    )
    for day in by_day:
        if day not in _DAYS_TO_GRAPH:
            raise UnsupportedRecurrence(f"Unbekannter Wochentag: {day}")

    return Rule(
        freq=freq,
        interval=max(1, int(parts.get("INTERVAL", "1") or 1)),
        by_day=by_day,
        by_month_day=_maybe_int(parts.get("BYMONTHDAY")),
        by_month=_maybe_int(parts.get("BYMONTH")),
        count=count,
        until=until,
    )


def _maybe_int(value: str | None) -> int | None:
    if not value:
        return None
    first = value.split(",")[0].strip()
    try:
        return int(first)
    except ValueError:
        return None


def _parse_until(value: str) -> datetime:
    """``UNTIL`` kommt als ``YYYYMMDDTHHMMSSZ`` oder als reines ``YYYYMMDD``."""
    raw = value.strip()
    try:
        if raw.endswith("Z"):
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        if "T" in raw:
            return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC"))
        return datetime.strptime(raw, "%Y%m%d").replace(
            hour=23, minute=59, second=59, tzinfo=ZoneInfo("UTC"))
    except ValueError as exc:
        raise UnsupportedRecurrence(f"UNTIL nicht lesbar: {value!r}") from exc


# ------------------------------------------------------------------ RRULE -> Graph

def to_graph(rrule: str, start: datetime, timezone: str) -> dict:
    """Baut das Graph-``recurrence``-Objekt.

    ``start`` ist der erste Termin der Serie — er liefert den Wochentag beziehungsweise
    den Monatstag, wenn die Regel ihn nicht ausdrücklich nennt, und das Startdatum der
    Spanne. ``timezone`` ist der **IANA**-Name der Anzeigezeitzone.
    """
    rule = parse_rrule(rrule)
    zone = ZoneInfo(timezone)
    local_start = start.astimezone(zone)

    pattern: dict = {"type": _FREQ_TO_GRAPH[rule.freq], "interval": rule.interval}

    if rule.freq == "WEEKLY":
        days = rule.by_day or (_WEEKDAY_BY_INDEX[local_start.weekday()],)
        pattern["daysOfWeek"] = [_DAYS_TO_GRAPH[d] for d in days]
    elif rule.freq == "MONTHLY":
        if rule.by_day:
            # Graph kennt dafuer einen eigenen Mustertyp - "jeden zweiten Dienstag".
            pattern["type"] = "relativeMonthly"
            pattern["daysOfWeek"] = [_DAYS_TO_GRAPH[d] for d in rule.by_day]
        else:
            pattern["dayOfMonth"] = rule.by_month_day or local_start.day
    elif rule.freq == "YEARLY":
        pattern["month"] = rule.by_month or local_start.month
        if rule.by_day:
            pattern["type"] = "relativeYearly"
            pattern["daysOfWeek"] = [_DAYS_TO_GRAPH[d] for d in rule.by_day]
        else:
            pattern["dayOfMonth"] = rule.by_month_day or local_start.day

    range_: dict = {
        "type": "noEnd",
        "startDate": local_start.date().isoformat(),
        "recurrenceTimeZone": timezone,
    }
    if rule.count:
        range_["type"] = "numbered"
        range_["numberOfOccurrences"] = rule.count
    elif rule.until:
        range_["type"] = "endDate"
        # Der Abschluss wird in Ortszeit gelesen - sonst faellt der letzte Termin
        # einer abends endenden Serie weg.
        range_["endDate"] = rule.until.astimezone(zone).date().isoformat()

    return {"pattern": pattern, "range": range_}


# ------------------------------------------------------------------ Graph -> RRULE

def from_graph(recurrence: dict, timezone: str, *,
               endless_end: datetime | None = None) -> str:
    """Baut die RFC-5545-Regel für TANSS.

    ``endless_end`` ist das Enddatum, das eine **endlose** Graph-Serie bekommt. Fehlt es
    bei einer solchen Serie, ist das ein Fehler und keine stille Annahme: TANSS würde die
    Regel ablehnen, und eine geratene Spanne wäre eine Behauptung über fremde Termine.
    """
    pattern = (recurrence or {}).get("pattern") or {}
    range_ = (recurrence or {}).get("range") or {}
    kind = pattern.get("type")

    freq = {
        "daily": "DAILY", "weekly": "WEEKLY",
        "absoluteMonthly": "MONTHLY", "relativeMonthly": "MONTHLY",
        "absoluteYearly": "YEARLY", "relativeYearly": "YEARLY",
    }.get(kind)
    if freq is None:
        raise UnsupportedRecurrence(f"Graph-Muster {kind!r} wird nicht unterstützt")

    out = [f"FREQ={freq}"]
    interval = int(pattern.get("interval") or 1)
    if interval > 1:
        out.append(f"INTERVAL={interval}")

    days = [_DAYS_TO_RRULE[d] for d in pattern.get("daysOfWeek", [])
            if d in _DAYS_TO_RRULE]
    if days:
        out.append("BYDAY=" + ",".join(days))
    if kind == "absoluteMonthly" and pattern.get("dayOfMonth"):
        out.append(f"BYMONTHDAY={int(pattern['dayOfMonth'])}")
    if kind in ("absoluteYearly", "relativeYearly"):
        if pattern.get("month"):
            out.append(f"BYMONTH={int(pattern['month'])}")
        if kind == "absoluteYearly" and pattern.get("dayOfMonth"):
            out.append(f"BYMONTHDAY={int(pattern['dayOfMonth'])}")

    range_type = range_.get("type")
    if range_type == "numbered" and range_.get("numberOfOccurrences"):
        out.append(f"COUNT={int(range_['numberOfOccurrences'])}")
    elif range_type == "endDate" and range_.get("endDate"):
        zone = ZoneInfo(range_.get("recurrenceTimeZone") or timezone)
        last = date.fromisoformat(str(range_["endDate"]))
        # Bis zum Ende des Tages in Ortszeit, dann nach UTC - so bleibt der letzte
        # Termin der Serie erhalten.
        until = datetime.combine(last, time(23, 59, 59), tzinfo=zone)
        out.append("UNTIL=" + until.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ"))
    else:
        if endless_end is None:
            raise UnsupportedRecurrence(
                "endlose Serie ohne vorgegebenes Enddatum — TANSS verlangt eines")
        out.append("UNTIL=" + endless_end.astimezone(ZoneInfo("UTC"))
                   .strftime("%Y%m%dT%H%M%SZ"))

    return ";".join(out)


def endless_cutoff(start: datetime, years: int) -> datetime:
    """Das Enddatum, das eine endlose Serie in TANSS bekommt.

    Wird bei jedem Lauf nachgezogen, solange die Serie in Graph endlos bleibt. Dadurch
    wirkt sie endlos, ohne eine Regel zu verletzen.
    """
    return start + timedelta(days=365 * max(1, years))
