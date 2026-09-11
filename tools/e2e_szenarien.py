"""Die Prüffälle des Ende-zu-Ende-Prüfstands.

Getrennt von der Mechanik in ``e2e_probe.py``: Dort stehen die Geländer und der
Zugriff auf beide Systeme, hier ausschließlich die Fachfragen.

**Jedes Szenario endet mit mindestens zwei stillen Läufen.** Ein Abgleich, der beim
zweiten Durchgang erneut schreibt, ist kaputt — auch wenn das Ergebnis beim ersten Mal
richtig aussah. Wo ein Schlüssel beim Schreiben wechseln könnte, sind es drei: Der
Fehler bei den Fahrt-Blöcken zeigte sich erst im dritten Lauf.
"""

from __future__ import annotations

import time

from e2e_probe import Befund, Welt, _fehler, _ok

from tanss_sync.domain.uid import canonical_uid

SCHREIBEND = ("create", "update", "delete", "link", "detach")


# --------------------------------------------------------------- Prüfwerkzeuge

def aktionen(welt: Welt, run_id: int) -> list[dict]:
    """Die schreibenden Aktionen eines Durchlaufs."""
    zeilen = welt.rt.state.connect().execute(
        "SELECT operation, outcome, side, travel_role, tanss_support_id, "
        "graph_event_id, changed_fields, reason FROM audit "
        "WHERE run_id = ? AND operation IN "
        "('create','update','delete','link','detach') ORDER BY id", (run_id,))
    return [dict(z) for z in zeilen]


def lauf(welt: Welt, *, nach_aenderung: bool = False) -> tuple[object, list[dict]]:
    """Ein Abgleich.

    ``nach_aenderung`` wartet vorher die Echo-Sperre ab. Nach einem eigenen
    Schreibvorgang verwirft der Abgleich für ``echo_suppression_seconds`` alles, was
    von der Gegenseite zurückkommt — sonst entstünde ein Hin und Her. Ein Prüfstand,
    der schneller misst als dieses Fenster, sieht deshalb **gar keine** Reaktion und
    hält ein funktionierendes Werkzeug für kaputt.
    """
    if nach_aenderung:
        time.sleep(welt.config.sync.echo_suppression_seconds + 3)
    bericht = welt.abgleichen()
    return bericht, aktionen(welt, bericht.run_id)


def kurz(getan: list[dict]) -> str:
    return ", ".join(f"{a['operation']}/{a['side']}/{a['travel_role']}" for a in getan)


def felder_von(zeile: dict) -> set[str]:
    """``changed_fields`` steht als Text in der Datenbank."""
    roh = (zeile.get("changed_fields") or "").strip("[]").replace('"', "")
    return {teil.strip() for teil in roh.split(",") if teil.strip()}


def still(welt: Welt, wieviele: int = 2) -> list[Befund]:
    befunde = []
    for nummer in range(1, wieviele + 1):
        _, getan = lauf(welt)
        name = f"Folgelauf {nummer} bleibt still"
        befunde.append(_ok(name) if not getan
                       else _fehler(name, "tat noch etwas: " + kurz(getan)))
    return befunde


# --------------------------------------------------------------------- Block 0

def nulllauf(welt: Welt) -> list[Befund]:
    """Der echte Kalender wird in Ruhe gelassen.

    Schlägt das fehl, ist jede weitere Messung wertlos: Ein Abgleich, der den Bestand
    anfasst, obwohl sich nichts geändert hat, überlagert alles, was danach kommt.
    """
    vorher = {e.id for e in welt.graph_heute()}
    befunde = still(welt, 2)

    nachher = {e.id for e in welt.graph_heute()}
    if nachher != vorher:
        befunde.append(_fehler(
            "Bestand unverändert",
            f"{len(nachher - vorher)} dazugekommen, {len(vorher - nachher)} verschwunden"))
    else:
        befunde.append(_ok("Bestand unverändert"))
    return befunde


# --------------------------------------------------------------------- Block 1

def a1_grundfall(welt: Welt) -> list[Befund]:
    """Ein Termin entsteht in TANSS und muss in Outlook ankommen — genau einmal."""
    start = welt.uhr(6, 0)
    support = welt.tanss_anlegen(
        titel="A1 Grundfall", start=start, dauer=30,
        text="E2E-A1 Grundfall\nZweite Zeile zur Textpruefung")

    _, getan = lauf(welt)
    anlagen = [a for a in getan if a["operation"] == "create" and a["side"] == "graph"]
    if len(anlagen) != 1:
        return [_fehler("genau eine Anlage in Outlook",
                        f"{len(anlagen)} statt 1 — {kurz(getan)}")]
    befunde = [_ok("genau eine Anlage in Outlook")]

    treffer = [e for e in welt.graph_marken() if "A1 Grundfall" in (e.subject or "")]
    if len(treffer) != 1:
        return [*befunde, _fehler("Termin liegt in Outlook", f"{len(treffer)} gefunden")]
    ereignis = treffer[0]

    # Der Betreff kommt aus der ersten Textzeile: outlookTitle ist bei allem leer,
    # was in TANSS entsteht. Dazu das Firmensuffix.
    if "(Firma: ProNet Systems GmbH)" not in (ereignis.subject or ""):
        befunde.append(_fehler("Firmensuffix im Betreff", f"Betreff: {ereignis.subject!r}"))
    else:
        befunde.append(_ok("Firmensuffix im Betreff"))
    if "Zweite Zeile" in (ereignis.subject or ""):
        befunde.append(_fehler("nur die erste Textzeile im Betreff",
                               f"Betreff: {ereignis.subject!r}"))
    else:
        befunde.append(_ok("nur die erste Textzeile im Betreff"))

    roh = welt.rt.graph.client.get(
        f"/users/{welt.postfach}/events/{ereignis.id}", scope=welt.postfach)
    if roh.get("showAs") != "busy" or roh.get("isAllDay"):
        befunde.append(_fehler(
            "gebucht und nicht ganztägig",
            f"showAs={roh.get('showAs')}, ganztägig={roh.get('isAllDay')}"))
    else:
        befunde.append(_ok("gebucht und nicht ganztägig"))

    # Ohne zurueckgeschriebene Kopplung faende ein Lauf ohne lokale Datenbank das
    # Paar nie wieder.
    danach = welt.tanss_holen(support)
    if not (danach and (danach.meta_infos or {}).get("SYNC_GROUP")):
        befunde.append(_fehler("Kopplung in TANSS hinterlegt", "SYNC_GROUP fehlt"))
    else:
        befunde.append(_ok("Kopplung in TANSS hinterlegt"))

    befunde.extend(still(welt, 2))
    welt.merker["a1_support"] = support
    welt.merker["a1_event"] = ereignis.id
    return befunde


def a2_zeiten(welt: Welt) -> list[Befund]:
    """Verschieben, verkürzen, verlängern — je ein Update auf demselben Termin."""
    support = welt.merker.get("a1_support")
    ereignis_id = welt.merker.get("a1_event")
    if not support:
        return [_fehler("A2 setzt auf A1 auf", "A1 hat keinen Termin hinterlassen")]

    befunde = []
    schritte = [
        ("verschoben", {"start": welt.uhr(6, 15)}, {"start", "end"}),
        # TANSS rundet die Dauer auf 15-Minuten-Schritte: Aus 10 wird 15, aus 20
        # wird 30. Ein Wert, der nicht auf dem Raster liegt, kommt als derselbe
        # zurueck wie vorher - und dann gibt es zu Recht nichts zu uebertragen.
        ("verkürzt", {"duration": 15}, {"end"}),
        ("verlängert", {"duration": 105}, {"end"}),
    ]
    for name, felder, erwartet in schritte:
        welt.tanss_aendern(support, **felder)
        _, getan = lauf(welt, nach_aenderung=True)
        änderungen = [a for a in getan if a["operation"] == "update"]
        if len(änderungen) != 1:
            befunde.append(_fehler(f"{name}: genau ein Update",
                                   f"{len(änderungen)} — {kurz(getan)}"))
            continue
        ist = felder_von(änderungen[0])
        fehlend = erwartet - ist
        if fehlend:
            befunde.append(_fehler(f"{name}: erwartete Felder",
                                   f"fehlt {sorted(fehlend)}, war {sorted(ist)}"))
        else:
            befunde.append(_ok(f"{name}: {', '.join(sorted(ist))}"))
        if änderungen[0]["graph_event_id"] not in (None, ereignis_id):
            befunde.append(_fehler(f"{name}: derselbe Outlook-Termin",
                                   "die Termin-Kennung hat gewechselt"))

    befunde.extend(still(welt, 2))
    return befunde


def a3_fahrten(welt: Welt) -> list[Befund]:
    """Fahrtzeiten an einem **bereits gekoppelten** Termin — der Fall, der zuletzt brach."""
    support = welt.merker.get("a1_support")
    if not support:
        return [_fehler("A3 setzt auf A1 auf", "kein Termin vorhanden")]

    welt.tanss_aendern(support, location="CUSTOMER",
                      duration_approach=10, duration_departure=10)
    _, getan = lauf(welt, nach_aenderung=True)

    fahrten = [a for a in getan
               if a["operation"] == "create" and a["travel_role"] != "main"]
    befunde = [_ok("zwei Fahrt-Blöcke entstehen") if len(fahrten) == 2
               else _fehler("zwei Fahrt-Blöcke entstehen",
                            f"{len(fahrten)} — {kurz(getan)}")]

    # Die eigenen Blöcke merken. Pauschal nach dem Betreff zu suchen fände auch
    # fremde Fahrt-Blöcke im Kalender — und ein Prüfstand, der fremde Termine misst,
    # meldet Fehler, die es nicht gibt.
    eigene = {a["graph_event_id"] for a in fahrten if a["graph_event_id"]}
    welt.merker["fahrt_bloecke"] = eigene
    blöcke = {e.subject for e in welt.graph_heute()
              if e.id in eigene}
    befunde.append(_ok("Anfahrt und Abfahrt liegen in Outlook")
                   if blöcke == {"Anfahrt", "Abfahrt"}
                   else _fehler("Anfahrt und Abfahrt liegen in Outlook",
                                f"gefunden: {sorted(blöcke)}"))

    # Die Kopplung des HAUPTTERMINS darf dabei nicht ueberschrieben worden sein -
    # alle drei Zeilen teilen sich die Support-Kennung.
    danach = welt.tanss_holen(support)
    gruppe = (danach.meta_infos or {}).get("SYNC_GROUP", "") if danach else ""
    haupt = [e for e in welt.graph_marken() if "A1 Grundfall" in (e.subject or "")]
    if haupt and gruppe:
        eigene = canonical_uid(haupt[0].ical_uid)
        if eigene and not gruppe.startswith(eigene[:32]):
            befunde.append(_fehler("Kopplung zeigt weiter auf den Haupttermin",
                                   "ein Fahrt-Block hat sie überschrieben"))
        else:
            befunde.append(_ok("Kopplung zeigt weiter auf den Haupttermin"))

    # Drei Laeufe: Der Schluesselwechsel einer Fahrt-Zeile zeigte sich erst im dritten.
    befunde.extend(still(welt, 3))
    return befunde


def a4_fahrt_entfernt(welt: Welt) -> list[Befund]:
    """Fahrtzeit auf 0 — die Blöcke gehen, der Termin bleibt."""
    support = welt.merker.get("a1_support")
    if not support:
        return [_fehler("A4 setzt auf A1 auf", "kein Termin vorhanden")]

    welt.tanss_aendern(support, duration_approach=0, duration_departure=0)

    # Der erste Lauf merkt nur vor - die Karenzzeit laeuft.
    lauf(welt, nach_aenderung=True)
    vorgemerkt = welt.rt.state.connect().execute(
        "SELECT COUNT(*) AS n FROM pending_deletions "
        "WHERE executed_at IS NULL AND cancelled_at IS NULL").fetchone()["n"]
    befunde = [_ok("erst vorgemerkt, nicht gelöscht") if vorgemerkt
               else _fehler("erst vorgemerkt, nicht gelöscht",
                            "es gab keine Vormerkung")]

    time.sleep(min(welt.config.safety.deletion_grace_seconds + 5, 120))
    _, zweiter = lauf(welt)

    löschungen = [a for a in zweiter
                  if a["operation"] == "delete" and a["outcome"] == "ok"]
    befunde.append(_ok("beide Fahrt-Blöcke werden entfernt") if len(löschungen) == 2
                   else _fehler("beide Fahrt-Blöcke werden entfernt",
                                f"{len(löschungen)} Löschungen — {kurz(zweiter)}"))

    eigene = welt.merker.get("fahrt_bloecke") or set()
    übrig = [e.subject for e in welt.graph_heute() if e.id in eigene]
    befunde.append(_ok("keine Fahrt-Blöcke mehr in Outlook") if not übrig
                   else _fehler("keine Fahrt-Blöcke mehr in Outlook", f"übrig: {übrig}"))

    befunde.append(_ok("der Haupttermin bleibt")
                   if any("A1 Grundfall" in (e.subject or "") for e in welt.graph_marken())
                   else _fehler("der Haupttermin bleibt", "er ist verschwunden"))

    befunde.extend(still(welt, 2))
    return befunde


BLOCK_0 = [("0.1", "Nulllauf über den echten Kalender", nulllauf)]

BLOCK_1 = [
    ("1.1", "Grundfall TANSS → Outlook", a1_grundfall),
    ("1.2", "Zeiten ändern", a2_zeiten),
    ("1.3", "Fahrtzeiten an gekoppeltem Termin", a3_fahrten),
    ("1.4", "Fahrtzeiten entfernen", a4_fahrt_entfernt),
]
