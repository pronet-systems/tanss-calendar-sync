"""Ende-zu-Ende-Prüfstand gegen echte Systeme.

Dieses Werkzeug legt Termine in TANSS und Outlook an, verändert sie, lässt den Abgleich
laufen und prüft, ob er das Richtige getan hat. Es arbeitet gegen **Produktivsysteme**,
und deshalb steht am Anfang nicht die Fachlogik, sondern das Geländer.

Vier Grenzen, die dieses Werkzeug nicht überschreitet — geprüft vor jedem einzelnen
Schreibvorgang, nicht einmal beim Start:

* **Niemals Teilnehmer.** Microsoft Graph verschickt beim Anlegen eines Termins mit
  Teilnehmern automatisch Einladungen. Das lässt sich nicht abschalten. Ein Testtermin,
  der versehentlich einen Teilnehmer trägt, schickt einem Kunden eine Mail — und die
  holt niemand zurück.
* **Nur heute.** Jeder Termin liegt am aktuellen Kalendertag. Ein Fehler im Prüfstand
  kann so nichts in der Zukunft anrichten, und ein vergessener Rest fällt am selben Tag
  auf.
* **Nur die eigene Firma.** Kein Kundendatensatz wird berührt.
* **Nur ein Postfach.** Das in der Konfiguration hinterlegte Testpostfach.

Jedes Szenario räumt hinter sich auf. Was der Prüfstand angelegt hat, trägt eine
Kennung im Betreff und wird am Ende gesucht und entfernt — auch das, was durch einen
Abbruch liegengeblieben ist.

Aufruf::

    python tools/e2e_probe.py --config .local/live.json            # Probelauf, schreibt nichts
    python tools/e2e_probe.py --config .local/live.json --wirklich # führt aus
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tanss_sync.config.store import ConfigStore
from tanss_sync.runtime import open_runtime
from tanss_sync.sync.engine import SyncEngine
from tanss_sync.tanss.models import TanssSupportWrite
from tanss_sync.util.timezone import _zone_of

#: Steht im Betreff jedes erzeugten Termins. Daran wird aufgeräumt.
MARKE = "SYNCPROBE"


class Gelaenderbruch(RuntimeError):
    """Ein Schreibvorgang hätte eine der vier Grenzen überschritten."""


# --------------------------------------------------------------------------- Geländer

def _pruefe_grenzen(*, start: dt.datetime, ende: dt.datetime | None,
                   teilnehmer: list | None, postfach: str, erlaubtes_postfach: str,
                   heute: dt.date, zone) -> None:
    """Vor **jedem** Schreibvorgang. Nicht einmal beim Start — hier."""
    if teilnehmer:
        raise Gelaenderbruch(
            "Ein Testtermin mit Teilnehmern würde Einladungsmails verschicken. "
            "Der Prüfstand legt niemals Termine mit Teilnehmern an.")
    if postfach != erlaubtes_postfach:
        raise Gelaenderbruch(
            f"Postfach {postfach} ist nicht das Testpostfach {erlaubtes_postfach}.")
    for zeitpunkt, was in ((start, "Beginn"), (ende, "Ende")):
        if zeitpunkt is None:
            continue
        tag = zeitpunkt.astimezone(zone).date()
        if tag != heute:
            raise Gelaenderbruch(
                f"{was} liegt am {tag:%d.%m.%Y}, erlaubt ist nur heute ({heute:%d.%m.%Y}).")


# --------------------------------------------------------------------------- Welt

@dataclass
class Welt:
    """Zugriff auf beide Systeme — mit Geländer und Aufräumliste."""

    rt: object
    config: object
    postfach: str
    employee_id: int
    zone: object
    heute: dt.date
    trocken: bool = False
    angelegt_tanss: list[int] = field(default_factory=list)
    angelegt_graph: list[str] = field(default_factory=list)
    #: Was ein Szenario dem naechsten weitergibt - etwa die Kennung seines Termins.
    merker: dict = field(default_factory=dict)

    @property
    def marke(self) -> str:
        return MARKE

    # ---------------------------------------------------------------- Zeiten

    def uhr(self, stunde: int, minute: int = 0) -> dt.datetime:
        """Ein Zeitpunkt am heutigen Tag, in Ortszeit gedacht, als UTC geliefert."""
        lokal = dt.datetime.combine(self.heute, dt.time(stunde, minute), tzinfo=self.zone)
        return lokal.astimezone(dt.UTC)

    # ---------------------------------------------------------------- TANSS

    def tanss_anlegen(self, *, titel: str, start: dt.datetime, dauer: int,
                      ort: str = "OFFICE", anfahrt: int = 0, abfahrt: int = 0,
                      typ: str = "APPOINTMENT_FIX", text: str = "") -> int | None:
        ende = start + dt.timedelta(minutes=dauer)
        _pruefe_grenzen(start=start, ende=ende, teilnehmer=None, postfach=self.postfach,
                       erlaubtes_postfach=self.postfach, heute=self.heute, zone=self.zone)
        if self.trocken:
            return None

        write = TanssSupportWrite()
        write.date = int(start.timestamp())
        write.duration = dauer
        write.employee_id = self.employee_id
        write.planning_type = typ
        write.location = ort
        write.outlook_title = f"{MARKE} {titel}"
        write.text = text
        write.internal = True
        if anfahrt:
            write.duration_approach = anfahrt
        if abfahrt:
            write.duration_departure = abfahrt
        # Keine companyId: Der tanss.x-Endpunkt traegt die EIGENE Firma ein.
        erzeugt = self.rt.tanss.create_support(write, prevent_notification=True)
        self.angelegt_tanss.append(erzeugt.id)
        return erzeugt.id

    def tanss_aendern(self, support_id: int, **felder) -> None:
        if self.trocken:
            return
        vorher = self.rt.tanss.get_support(support_id)
        write = TanssSupportWrite()
        # metaInfos IMMER vollstaendig mitschreiben - eine Teilmenge loescht den Rest.
        write.meta_infos = dict(vorher.meta_infos or {})
        for name, wert in felder.items():
            if name == "start":
                _pruefe_grenzen(start=wert, ende=None, teilnehmer=None,
                               postfach=self.postfach, erlaubtes_postfach=self.postfach,
                               heute=self.heute, zone=self.zone)
                write.date = int(wert.timestamp())
            else:
                setattr(write, name, wert)
        self.rt.tanss.update_support(support_id, write)

    def tanss_loeschen(self, support_id: int) -> None:
        if self.trocken:
            return
        # Beim Aufraeumen zaehlt nur das Ergebnis: Ein bereits entfernter Termin ist
        # kein Fehler, und ein Abbruch hier liesse den Rest stehen.
        with contextlib.suppress(Exception):
            self.rt.tanss.delete_support(support_id)
        if support_id in self.angelegt_tanss:
            self.angelegt_tanss.remove(support_id)

    def tanss_holen(self, support_id: int):
        try:
            return self.rt.tanss.get_support(support_id)
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- Outlook

    def graph_anlegen(self, *, titel: str, start: dt.datetime, dauer: int,
                      zeigen_als: str = "busy", ganztaegig: bool = False,
                      text: str = "", ort: str = "") -> str | None:
        ende = start + dt.timedelta(minutes=dauer)
        _pruefe_grenzen(start=start, ende=ende, teilnehmer=None, postfach=self.postfach,
                       erlaubtes_postfach=self.postfach, heute=self.heute, zone=self.zone)
        if self.trocken:
            return None

        payload = {
            "subject": f"{MARKE} {titel}",
            "body": {"contentType": "text", "content": text},
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {"dateTime": ende.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "showAs": zeigen_als,
            "isAllDay": ganztaegig,
            "isReminderOn": False,
            # attendees fehlt hier bewusst und wird nie gesetzt.
        }
        if ort:
            payload["location"] = {"displayName": ort}
        erzeugt = self.rt.graph.create_event(self.postfach, payload,
                                             transaction_id=f"probe-{time.time_ns()}")
        self.angelegt_graph.append(erzeugt.id)
        return erzeugt.id

    def graph_aendern(self, event_id: str, payload: dict) -> None:
        if self.trocken:
            return
        if "attendees" in payload:
            raise Gelaenderbruch("Teilnehmer werden nie geschrieben.")
        for feld in ("start", "end"):
            if feld in payload:
                roh = payload[feld]["dateTime"][:19]
                zeitpunkt = dt.datetime.fromisoformat(roh).replace(tzinfo=dt.UTC)
                _pruefe_grenzen(start=zeitpunkt, ende=None, teilnehmer=None,
                               postfach=self.postfach, erlaubtes_postfach=self.postfach,
                               heute=self.heute, zone=self.zone)
        self.rt.graph.client.patch(
            f"/users/{self.postfach}/events/{event_id}", payload, scope=self.postfach)

    def graph_loeschen(self, event_id: str) -> None:
        if self.trocken:
            return
        with contextlib.suppress(Exception):
            self.rt.graph.client.delete(
                f"/users/{self.postfach}/events/{event_id}", scope=self.postfach)
        if event_id in self.angelegt_graph:
            self.angelegt_graph.remove(event_id)

    def graph_heute(self) -> list:
        von = dt.datetime.combine(self.heute, dt.time.min, tzinfo=self.zone).astimezone(dt.UTC)
        return self.rt.graph.list_calendar_view(self.postfach, von,
                                                von + dt.timedelta(days=1))

    def graph_marken(self) -> list:
        """Alle vom Prüfstand erzeugten Outlook-Termine von heute."""
        return [e for e in self.graph_heute() if MARKE in (e.subject or "")]

    def tanss_marken(self) -> list:
        von = dt.datetime.combine(self.heute, dt.time.min, tzinfo=self.zone).astimezone(dt.UTC)
        seite = self.rt.tanss.list_appointments(
            self.employee_id, von, von + dt.timedelta(days=1))
        return [s for s in seite.items
                if MARKE in (s.outlook_title or "") or MARKE in (s.text or "")]

    # ---------------------------------------------------------------- Abgleich

    def abgleichen(self) -> object:
        """Ein Durchlauf. Gibt den Bericht zurück."""
        if self.trocken:
            return None
        engine = SyncEngine(self.config, self.rt.tanss, self.rt.graph, self.rt.state)
        return engine.run_once()

    def aufraeumen(self) -> int:
        """Entfernt alles, was den Prüfstand-Marker trägt — auf beiden Seiten.

        Gesucht wird über den Marker im Betreff, nicht über die Liste der in diesem
        Lauf erzeugten Kennungen: Ein abgebrochener Lauf hinterlässt Reste, die in
        keiner Liste stehen. Der Marker findet auch die.
        """
        if self.trocken:
            return 0

        supports = {s.id for s in self.tanss_marken()}
        ereignisse = {e.id for e in self.graph_marken()}

        # Fahrt-Blöcke heißen schlicht „Anfahrt" und „Abfahrt" — sie tragen den Marker
        # **nicht** und wären über den Betreff nie zu finden. Gefunden werden sie über
        # die Verknüpfung: Alle drei Zeilen teilen sich die Support-Kennung. Ohne
        # diesen Schritt bliebe nach jedem Fahrt-Szenario ein Block im Kalender stehen.
        db = self.rt.state.connect()
        for support_id in list(supports):
            for zeile in db.execute(
                    "SELECT graph_event_id FROM links WHERE tanss_support_id = ? "
                    "AND mailbox = ? AND travel_role != 'main' "
                    "AND graph_event_id IS NOT NULL", (support_id, self.postfach)):
                ereignisse.add(zeile["graph_event_id"])

        for ereignis_id in ereignisse:
            self.graph_loeschen(ereignis_id)
        for support_id in supports:
            self.tanss_loeschen(support_id)

        # Die Verknüpfungen der Testtermine ebenfalls — aber **nur** diese. Bliebe eine
        # stehen, hielte der nächste Lauf ihren Termin für gelöscht und trüge ihn in
        # den Löschpfad; und eine zu weit gefasste Bedingung träfe die echten Termine
        # des Postfachs.
        # Offene Löschvormerkungen der Testtermine zuerst — danach sind ihre
        # Verknüpfungen weg und die Zuordnung über ``uid`` wäre nicht mehr möglich.
        # Die frühere Fassung fragte ``uid IN (SELECT uid FROM links ...)`` ab und
        # traf damit die Vormerkungen **aller** Termine des Postfachs, nicht nur die
        # des Prüfstands.
        uids = set()
        for support_id in supports:
            for zeile in db.execute(
                    "SELECT uid FROM links WHERE tanss_support_id = ? AND mailbox = ?",
                    (support_id, self.postfach)):
                uids.add(zeile["uid"])
        for uid in uids:
            db.execute("DELETE FROM pending_deletions WHERE mailbox = ? AND uid = ?",
                       (self.postfach, uid))

        for support_id in supports:
            db.execute("DELETE FROM links WHERE tanss_support_id = ? AND mailbox = ?",
                       (support_id, self.postfach))
        for ereignis_id in ereignisse:
            db.execute("DELETE FROM links WHERE graph_event_id = ? AND mailbox = ?",
                       (ereignis_id, self.postfach))

        return len(supports) + len(ereignisse)


# --------------------------------------------------------------------------- Szenarien

@dataclass
class Befund:
    name: str
    bestanden: bool
    meldung: str = ""


@dataclass
class Szenario:
    nummer: int
    name: str
    beschreibung: str
    ablauf: Callable[[Welt], list[Befund]]


def _ok(name: str) -> Befund:
    return Befund(name, True)


def _fehler(name: str, meldung: str) -> Befund:
    return Befund(name, False, meldung)


# --------------------------------------------------------------------------- Ablauf

def _welt_bauen(config_pfad: str, trocken: bool) -> tuple:
    config = ConfigStore(config_pfad).load()
    benutzer = [u for u in config.users if u.enabled]
    if len(benutzer) != 1:
        raise Gelaenderbruch(
            f"Der Pruefstand arbeitet mit genau EINEM freigeschalteten Benutzer, "
            f"gefunden: {len(benutzer)}. Sonst schreibt ein Szenario in fremde Kalender.")
    if config.sync.dry_run and not trocken:
        raise Gelaenderbruch(
            "In der Konfiguration steht dry_run. Dann schreibt der Abgleich nichts, "
            "und jedes Szenario meldet falsche Befunde.")

    zone = _zone_of(config.microsoft.timezone)
    laufzeit = open_runtime(config_pfad)
    rt = laufzeit.__enter__()
    welt = Welt(rt=rt, config=config, postfach=benutzer[0].mailbox,
                employee_id=benutzer[0].tanss_employee_id, zone=zone,
                heute=dt.datetime.now(zone).date(), trocken=trocken)
    return welt, laufzeit


def main(argv: list[str] | None = None) -> int:
    zerleger = argparse.ArgumentParser(description="Ende-zu-Ende-Pruefstand")
    zerleger.add_argument("--config", required=True)
    zerleger.add_argument("--block", action="append", default=None,
                          help="0 oder 1; mehrfach angebbar. Ohne Angabe: alle")
    zerleger.add_argument("--wirklich", action="store_true",
                          help="Ohne diese Angabe wird nichts geschrieben")
    args = zerleger.parse_args(argv)

    import e2e_szenarien as sz

    bloecke = {"0": sz.BLOCK_0, "1": sz.BLOCK_1, "2": sz.BLOCK_2,
               "3": sz.BLOCK_3, "4": sz.BLOCK_4, "5": sz.BLOCK_5}
    gewaehlt = args.block or sorted(bloecke)
    szenarien = [s for b in gewaehlt for s in bloecke.get(b, [])]

    welt, laufzeit = _welt_bauen(args.config, trocken=not args.wirklich)
    print(f"Postfach   : {welt.postfach}")
    print(f"Mitarbeiter: {welt.employee_id}")
    print(f"Tag        : {welt.heute:%d.%m.%Y}")
    print(f"Fenster    : {welt.config.sync.window_days_past} Tage zurueck bis "
          f"{welt.config.sync.window_days_future} voraus")
    print(f"Modus      : {'PROBELAUF - es wird nichts geschrieben' if welt.trocken else 'SCHREIBEND'}")
    print()

    if welt.trocken:
        for nummer, name, _ in szenarien:
            print(f"  {nummer}  {name}")
        print(f"\n{len(szenarien)} Szenarien wuerden laufen. Mit --wirklich ausfuehren.")
        laufzeit.__exit__(None, None, None)
        return 0

    alle: list[tuple[str, str, Befund]] = []
    try:
        vorreste = welt.aufraeumen()
        if vorreste:
            print(f"[Vorlauf] {vorreste} Reste eines frueheren Laufs entfernt\n")

        for nummer, name, ablauf in szenarien:
            print(f"--- {nummer}  {name}")
            try:
                befunde = ablauf(welt)
            except Exception as exc:  # noqa: BLE001
                befunde = [_fehler("Szenario lief durch", f"{type(exc).__name__}: {exc}")]
            for b in befunde:
                zeichen = "ok  " if b.bestanden else "FEHL"
                print(f"    [{zeichen}] {b.name}" + (f" - {b.meldung}" if b.meldung else ""))
                alle.append((nummer, name, b))
            print()
    finally:
        entfernt = welt.aufraeumen()
        print(f"[Aufraeumen] {entfernt} Testtermine entfernt")
        laufzeit.__exit__(None, None, None)

    fehler = [a for a in alle if not a[2].bestanden]
    print()
    print(f"{len(alle) - len(fehler)} von {len(alle)} Pruefungen bestanden")
    if fehler:
        print("\nNicht bestanden:")
        for nummer, name, b in fehler:
            print(f"  {nummer} {name}: {b.name} - {b.meldung}")
    return 1 if fehler else 0


if __name__ == "__main__":
    raise SystemExit(main())
