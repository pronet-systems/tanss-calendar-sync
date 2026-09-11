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


# --------------------------------------------------------------------- Block 2

def b1_outlook_nach_tanss(welt: Welt) -> list[Befund]:
    """Ein in Outlook eingetragener Termin muss in TANSS ankommen."""
    ereignis = welt.graph_anlegen(titel="B1 aus Outlook", start=welt.uhr(10, 0),
                                  dauer=30, text="Aus Outlook eingetragen")
    _, getan = lauf(welt, nach_aenderung=True)
    anlagen = [a for a in getan if a["operation"] == "create" and a["side"] == "tanss"]
    if len(anlagen) != 1:
        return [_fehler("genau eine Anlage in TANSS", f"{len(anlagen)} — {kurz(getan)}")]
    befunde = [_ok("genau eine Anlage in TANSS")]

    treffer = [s for s in welt.tanss_marken() if "B1 aus Outlook" in (s.outlook_title or "")]
    if len(treffer) != 1:
        return [*befunde, _fehler("Termin liegt in TANSS", f"{len(treffer)} gefunden")]
    support = treffer[0]

    # Ein selbst eingetragener Termin ohne Eingeladene ist ein FESTER Termin, keine
    # Vormerkung - Graph meldet responseStatus "none" in beiden Lagen.
    befunde.append(_ok("fester Termin, keine Vormerkung")
                   if str(support.planning_type) == "APPOINTMENT_FIX"
                   else _fehler("fester Termin, keine Vormerkung",
                                f"planningType={support.planning_type}"))
    befunde.append(_ok("eigene Firma eingetragen") if support.company_id
                   else _fehler("eigene Firma eingetragen", "companyId fehlt"))
    befunde.append(_ok("Kopplung in TANSS hinterlegt")
                   if (support.meta_infos or {}).get("SYNC_GROUP")
                   else _fehler("Kopplung in TANSS hinterlegt", "SYNC_GROUP fehlt"))

    befunde.extend(still(welt, 2))
    welt.merker["b1_event"] = ereignis
    welt.merker["b1_support"] = support.id
    return befunde


def b2_outlook_aendern(welt: Welt) -> list[Befund]:
    """Eine Änderung in Outlook muss nach TANSS durchschlagen."""
    ereignis = welt.merker.get("b1_event")
    if not ereignis:
        return [_fehler("B2 setzt auf B1 auf", "kein Termin vorhanden")]

    welt.graph_aendern(ereignis, {"subject": f"{welt.marke} B2 in Outlook geaendert"})
    _, getan = lauf(welt, nach_aenderung=True)
    änderungen = [a for a in getan if a["operation"] == "update" and a["side"] == "tanss"]
    befunde = [_ok("genau ein Update in TANSS") if len(änderungen) == 1
               else _fehler("genau ein Update in TANSS", f"{len(änderungen)} — {kurz(getan)}")]

    support = welt.tanss_holen(welt.merker.get("b1_support"))
    if support and "B2 in Outlook geaendert" in (support.outlook_title or ""):
        befunde.append(_ok("neuer Betreff steht in TANSS"))
    else:
        befunde.append(_fehler("neuer Betreff steht in TANSS",
                               f"Titel: {support.outlook_title if support else '—'!r}"))

    befunde.extend(still(welt, 2))
    return befunde


# --------------------------------------------------------------------- Block 3

def c1_kollision(welt: Welt) -> list[Befund]:
    """Beide Seiten ändern denselben Termin zwischen zwei Läufen.

    Nur der eingestellte Gewinner darf schreiben. Schriebe auch die Verliererseite,
    drehten sich beide gegenseitig zurück — bei jedem Lauf aufs Neue.
    """
    support = welt.merker.get("b1_support")
    ereignis = welt.merker.get("b1_event")
    if not (support and ereignis):
        return [_fehler("C1 setzt auf B1 auf", "kein Termin vorhanden")]

    gewinner = welt.config.sync.conflict_winner
    welt.tanss_aendern(support, outlook_title=f"{welt.marke} C1 aus TANSS")
    welt.graph_aendern(ereignis, {"subject": f"{welt.marke} C1 aus Outlook"})

    _, getan = lauf(welt, nach_aenderung=True)
    nach_tanss = [a for a in getan if a["side"] == "tanss" and a["operation"] == "update"]
    nach_graph = [a for a in getan if a["side"] == "graph" and a["operation"] == "update"]

    befunde = []
    if gewinner == "tanss":
        befunde.append(_ok("nur Richtung Outlook geschrieben") if not nach_tanss
                       else _fehler("nur Richtung Outlook geschrieben",
                                    "es wurde auch nach TANSS geschrieben"))
        befunde.append(_ok("die Gewinnerseite hat geschrieben") if nach_graph
                       else _fehler("die Gewinnerseite hat geschrieben", "nichts passiert"))
    else:
        befunde.append(_ok("nur Richtung TANSS geschrieben") if not nach_graph
                       else _fehler("nur Richtung TANSS geschrieben",
                                    "es wurde auch nach Outlook geschrieben"))

    befunde.extend(still(welt, 2))
    return befunde


# --------------------------------------------------------------------- Block 4

def d1_in_outlook_geloescht(welt: Welt) -> list[Befund]:
    """In Outlook entfernt — in TANSS muss es folgen, aber erst mit Nachweis."""
    ereignis = welt.merker.get("b1_event")
    support = welt.merker.get("b1_support")
    if not (ereignis and support):
        return [_fehler("D1 setzt auf B1 auf", "kein Termin vorhanden")]

    welt.graph_loeschen(ereignis)
    lauf(welt)
    offen = welt.rt.state.connect().execute(
        "SELECT COUNT(*) AS n FROM pending_deletions "
        "WHERE executed_at IS NULL AND cancelled_at IS NULL").fetchone()["n"]
    befunde = [_ok("erst vorgemerkt, nicht gelöscht") if offen
               else _fehler("erst vorgemerkt, nicht gelöscht", "keine Vormerkung")]

    time.sleep(welt.config.safety.deletion_grace_seconds + 5)
    _, getan = lauf(welt)
    löschungen = [a for a in getan
                  if a["operation"] == "delete" and a["outcome"] == "ok"]
    befunde.append(_ok("in TANSS gelöscht") if löschungen
                   else _fehler("in TANSS gelöscht", f"nichts — {kurz(getan)}"))

    befunde.append(_ok("Termin ist in TANSS weg") if welt.tanss_holen(support) is None
                   else _fehler("Termin ist in TANSS weg", "er steht noch"))

    sicherung = welt.rt.state.connect().execute(
        "SELECT COUNT(*) AS n FROM deleted_backup WHERE tanss_support_id = ?",
        (support,)).fetchone()["n"]
    befunde.append(_ok("vor der Löschung gesichert") if sicherung
                   else _fehler("vor der Löschung gesichert", "keine Sicherung"))

    befunde.extend(still(welt, 2))
    welt.merker.pop("b1_event", None)
    welt.merker.pop("b1_support", None)
    return befunde


def d2_in_tanss_geloescht(welt: Welt) -> list[Befund]:
    """Umgekehrt: in TANSS entfernt, der Outlook-Termin muss folgen."""
    support = welt.tanss_anlegen(titel="D2 Loeschprobe", start=welt.uhr(11, 0),
                                 dauer=30, text="D2 Loeschprobe")
    lauf(welt)
    treffer = [e for e in welt.graph_marken() if "D2 Loeschprobe" in (e.subject or "")]
    if not treffer:
        return [_fehler("D2: Termin kam in Outlook an", "er fehlt")]
    ereignis_id = treffer[0].id

    welt.tanss_loeschen(support)
    lauf(welt)
    time.sleep(welt.config.safety.deletion_grace_seconds + 5)
    _, getan = lauf(welt)

    löschungen = [a for a in getan
                  if a["operation"] == "delete" and a["outcome"] == "ok"]
    befunde = [_ok("in Outlook gelöscht") if löschungen
               else _fehler("in Outlook gelöscht", f"nichts — {kurz(getan)}")]
    befunde.append(_ok("Termin ist in Outlook weg")
                   if not any(e.id == ereignis_id for e in welt.graph_heute())
                   else _fehler("Termin ist in Outlook weg", "er steht noch"))

    befunde.extend(still(welt, 2))
    return befunde


# --------------------------------------------------------------------- Block 5

def e1_frei_wird_uebergangen(welt: Welt) -> list[Befund]:
    """Ein Outlook-Termin mit Status *Frei* geht nicht nach TANSS.

    Damit lassen sich bewusst Einträge führen, die der Abgleich nichts angeht.
    """
    welt.graph_anlegen(titel="E1 frei", start=welt.uhr(12, 0), dauer=30,
                       zeigen_als="free")
    _, getan = lauf(welt, nach_aenderung=True)
    nach_tanss = [a for a in getan if a["side"] == "tanss"]
    befunde = [_ok("nichts nach TANSS geschrieben") if not nach_tanss
               else _fehler("nichts nach TANSS geschrieben", kurz(getan))]
    befunde.append(_ok("kein Termin in TANSS entstanden")
                   if not any("E1 frei" in (s.outlook_title or "")
                              for s in welt.tanss_marken())
                   else _fehler("kein Termin in TANSS entstanden", "es gibt einen"))
    befunde.extend(still(welt, 2))
    return befunde


def e2_auf_frei_gesetzt(welt: Welt) -> list[Befund]:
    """Ein **gekoppelter** Termin wird auf *Frei* gesetzt: entkoppeln, nicht löschen.

    Ohne diese Regel wäre „auf Frei setzen" ein stiller Löschbefehl für den
    TANSS-Datensatz.
    """
    support = welt.tanss_anlegen(titel="E2 Entkopplung", start=welt.uhr(13, 0),
                                 dauer=30, text="E2 Entkopplung")
    lauf(welt)
    treffer = [e for e in welt.graph_marken() if "E2 Entkopplung" in (e.subject or "")]
    if not treffer:
        return [_fehler("E2: Termin kam in Outlook an", "er fehlt")]

    welt.graph_aendern(treffer[0].id, {"showAs": "free"})
    _, getan = lauf(welt, nach_aenderung=True)

    entkopplungen = [a for a in getan if a["operation"] == "detach"]
    löschungen = [a for a in getan if a["operation"] == "delete"]
    befunde = [_ok("entkoppelt statt gelöscht") if entkopplungen and not löschungen
               else _fehler("entkoppelt statt gelöscht", kurz(getan) or "nichts geschah")]
    befunde.append(_ok("der TANSS-Termin bleibt bestehen")
                   if welt.tanss_holen(support) is not None
                   else _fehler("der TANSS-Termin bleibt bestehen", "er ist weg"))
    befunde.extend(still(welt, 2))
    return befunde


BLOCK_0 = [("0.1", "Nulllauf über den echten Kalender", nulllauf)]

BLOCK_1 = [
    ("1.1", "Grundfall TANSS → Outlook", a1_grundfall),
    ("1.2", "Zeiten ändern", a2_zeiten),
    ("1.3", "Fahrtzeiten an gekoppeltem Termin", a3_fahrten),
    ("1.4", "Fahrtzeiten entfernen", a4_fahrt_entfernt),
]

BLOCK_2 = [
    ("2.1", "Grundfall Outlook → TANSS", b1_outlook_nach_tanss),
    ("2.2", "In Outlook ändern", b2_outlook_aendern),
]

BLOCK_3 = [
    ("3.1", "Kollision: beide Seiten geändert", c1_kollision),
]

BLOCK_4 = [
    ("4.1", "In Outlook gelöscht", d1_in_outlook_geloescht),
    ("4.2", "In TANSS gelöscht", d2_in_tanss_geloescht),
]

BLOCK_5 = [
    ("5.1", "Status Frei wird übergangen", e1_frei_wird_uebergangen),
    ("5.2", "Auf Frei gesetzt: entkoppeln statt löschen", e2_auf_frei_gesetzt),
]
