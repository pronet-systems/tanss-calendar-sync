"""Zeitumrechnung zwischen TANSS, Graph und dem internen Modell.

Intern ist jede Zeit ein timezone-aware ``datetime`` in UTC. Nach außen:

* **TANSS** rechnet in Unix-Sekunden.
* **Graph** bekommt grundsätzlich ``timeZone: "UTC"``. Das ist eindeutig und erspart
  die Umrechnung zwischen Windows- und IANA-Namen — ``ZoneInfo("W. Europe Standard
  Time")`` wirft, und eine Zuordnungstabelle wäre eine Fehlerquelle ohne Gegenwert.
  Die konfigurierte Zeitzone dient nur der Anzeige und der Prüfung im Setup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Nur fuer Anzeige und Setup-Pruefung - nicht fuer das Schreiben an Graph.
_WINDOWS_TO_IANA = {
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "UTC": "UTC",
}


class TimeConverter:
    def __init__(self, display_timezone: str = "W. Europe Standard Time") -> None:
        self.display_timezone = display_timezone

    # ---------------------------------------------------------------- TANSS

    @staticmethod
    def tanss_to_utc(seconds: int) -> datetime:
        return datetime.fromtimestamp(seconds, UTC)

    @staticmethod
    def utc_to_tanss(value: datetime) -> int:
        return int(value.astimezone(UTC).timestamp())

    @staticmethod
    def end_of(start: datetime, duration_minutes: int) -> datetime:
        """TANSS kennt kein Endfeld — das Ende ergibt sich aus Start plus Dauer."""
        return start + timedelta(minutes=duration_minutes)

    # ---------------------------------------------------------------- Graph

    @staticmethod
    def to_graph(value: datetime) -> dict[str, str]:
        """Immer UTC. Nie ein Windows- oder IANA-Name."""
        stamp = value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        return {"dateTime": stamp, "timeZone": "UTC"}

    @staticmethod
    def from_graph(payload: dict) -> datetime | None:
        """Liest ``{dateTime, timeZone}``. Graph liefert ohne Zeitzonenangabe UTC."""
        if not payload or not payload.get("dateTime"):
            return None
        raw = payload["dateTime"]
        # Graph haengt gelegentlich Sub-Sekunden an, die fromisoformat vor 3.11 stoerten.
        if "." in raw:
            head, _, tail = raw.partition(".")
            raw = head + "." + tail[:6]
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            zone = payload.get("timeZone") or "UTC"
            parsed = parsed.replace(tzinfo=_zone_of(zone))
        return parsed.astimezone(UTC)

    @staticmethod
    def graph_window(value: datetime) -> str:
        """Format für ``startDateTime``/``endDateTime`` in ``calendarView``."""
        return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"

    # ---------------------------------------------------------------- Anzeige

    def local(self, value: datetime) -> datetime:
        return value.astimezone(_zone_of(self.display_timezone))

    def is_known_timezone(self) -> bool:
        """Ob die konfigurierte Zeitzone auflösbar ist — für ``doctor``."""
        return _zone_of(self.display_timezone) is not UTC


def _zone_of(name: str) -> tzinfo:
    """Akzeptiert Windows- und IANA-Schreibweise.

    Der Rückfall ist ``datetime.UTC``, nicht ``ZoneInfo("UTC")``: Fehlt die
    Zeitzonendatenbank ganz — unter Windows ohne ``tzdata`` der Normalfall —, wirft
    auch ``ZoneInfo("UTC")``. Ein Rückfall, der dieselbe Abhängigkeit braucht wie das,
    was gerade fehlgeschlagen ist, hilft nicht. Die Anzeige läuft dann eben in UTC,
    statt dass das Werkzeug abstürzt.
    """
    if name in _WINDOWS_TO_IANA:
        name = _WINDOWS_TO_IANA[name]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ModuleNotFoundError, KeyError):
        return UTC
