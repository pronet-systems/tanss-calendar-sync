# TANSS Calendar Sync

Bidirektionaler Terminabgleich zwischen **TANSS** und **Microsoft 365**.
Ein Kommandozeilenwerkzeug in Python, das als Dienst unter Linux läuft.

Termine, die in TANSS eingetragen werden, erscheinen im Outlook-Kalender des zugehörigen
Mitarbeiters — und umgekehrt. Änderungen und Löschungen werden in beide Richtungen
übertragen. Abwesenheiten wandern von TANSS nach Outlook.

> **Status:** In Entwicklung. Die Schnittstellen zu beiden Systemen sind implementiert und
> gegen ein Produktivsystem geprüft; einzelne Funktionen (Terminserien, An-/Abfahrten) sind
> noch nicht vollständig. Für den produktiven Einsatz sollte zunächst ein einzelner
> Testbenutzer eingerichtet und beobachtet werden.

---

## Inhalt

- [Was das Werkzeug tut](#was-das-werkzeug-tut)
- [Was synchronisiert wird](#was-synchronisiert-wird)
- [Voraussetzungen](#voraussetzungen)
- [Installation unter Ubuntu](#installation-unter-ubuntu)
- [Microsoft 365 vorbereiten](#microsoft-365-vorbereiten)
- [TANSS vorbereiten](#tanss-vorbereiten)
- [Einrichtung](#einrichtung)
- [Konfiguration](#konfiguration)
- [Kommandos](#kommandos)
- [Betrieb](#betrieb)
- [Protokollierung](#protokollierung)
- [FAQ](#faq)
- [Lizenz](#lizenz)

---

## Was das Werkzeug tut

- **Bidirektionaler Abgleich.** Termine, Terminvormerkungen und deren Änderungen laufen in
  beide Richtungen. Abwesenheiten nur von TANSS nach Microsoft 365.
- **Benutzer werden automatisch zugeordnet.** TANSS-Mitarbeiter werden über ihre
  E-Mail-Adresse dem passenden Postfach zugeordnet — auch später, wenn neue Mitarbeiter
  in beiden Systemen dazukommen.
- **Kein Zugriff auf Zugangsdaten.** Es wird genau ein TANSS-Token benötigt, das der Dienst
  danach selbstständig erneuert. Passwörter werden nie gespeichert oder abgefragt.
- **Löschschutz.** Termine werden nur dann gelöscht, wenn eine Löschung tatsächlich
  beobachtet wurde. Ein entfernter Benutzer, ein Fehler oder eine leere Antwort führen
  niemals zu Löschungen.
- **Vollständiges Protokoll.** Jede schreibende Operation wird mit Begründung festgehalten.
  Die Geschichte eines einzelnen Termins lässt sich über beide Systeme hinweg abrufen.
- **Läuft unbeaufsichtigt.** Als systemd-Dienst, neustartfest, ohne eingehende
  Netzwerkfreigaben.

---

## Was synchronisiert wird

| | TANSS → Microsoft 365 | Microsoft 365 → TANSS |
|---|---|---|
| Termine | ja | ja |
| Terminvormerkungen | ja (als normaler Termin) | — |
| Private Termine | ja (als privat markiert) | ja (als privater Termin) |
| Änderungen an bestehenden Terminen | ja | ja |
| Löschungen | ja | ja |
| Abwesenheiten (Urlaub, Krankheit, Abwesenheit, Überstunden) | ja | **nein** |
| Getätigte Leistungen | **nein** | — |
| Teams-Besprechungen | — | ja |

**Nicht übernommen werden:**

- Termine, die vor der Aktivierung eines Benutzers bereits bestanden.
- Termine außerhalb des konfigurierten Zeitfensters.
- Outlook-Termine mit dem Status **Frei** bzw. **Verfügbar**. Damit lassen sich bewusst
  Einträge anlegen, die vom Abgleich ausgenommen bleiben sollen.
- Outlook-Termine mit gesetztem Haken **Ganztägig**. Siehe [FAQ](#wie-bekomme-ich-einen-ganztägigen-termin-synchronisiert).
- Abgesagte Termine.

---

## Voraussetzungen

**Auf dem Server**

- Ubuntu 22.04 LTS oder neuer (andere Distributionen mit systemd funktionieren ebenfalls)
- Python 3.11 oder neuer
- Ausgehende HTTPS-Verbindungen zu `graph.microsoft.com` und zur eigenen TANSS-Instanz
- Keine eingehenden Freigaben nötig

**In TANSS**

- Ein API-Token für externe Anbindungen
- Ein Benutzerkonto mit dem Recht *„Darf API-Tokens für ext. Anbindungen erzeugen"*,
  damit der Dienst sein Token selbst erneuern kann
- Mitarbeiter mit gepflegter E-Mail-Adresse

**In Microsoft 365**

- Eine App-Registrierung in Microsoft Entra ID
- Zwei Anwendungsberechtigungen mit Administratorzustimmung:
  `Calendars.ReadWrite` für die Kalender und `User.Read.All` für die Zuordnung von
  Mitarbeitern zu Postfächern
- Ein Zertifikat oder Client-Secret für die App

---

## Installation unter Ubuntu

```bash
git clone https://github.com/pronet-systems/tanss-calendar-sync.git
cd tanss-calendar-sync
sudo ./deploy/install.sh
```

Das Skript richtet alles ein, was für den unbeaufsichtigten Betrieb nötig ist:

| Schritt | Ergebnis |
|---|---|
| Voraussetzungen prüfen | Python-Version, `venv`, systemd |
| Dienstbenutzer anlegen | `tanss-sync`, ohne Login-Shell und ohne Home-Verzeichnis |
| Verzeichnisse anlegen | siehe Tabelle unten |
| Anwendung installieren | eigene virtuelle Python-Umgebung unter `/opt/tanss-calendar-sync/.venv` |
| systemd-Unit installieren | `/etc/systemd/system/tanss-calendar-sync.service` |
| Dienst aktivieren | `systemctl enable` — **damit startet der Dienst nach jedem Reboot automatisch** |

| Verzeichnis | Inhalt | Rechte |
|---|---|---|
| `/opt/tanss-calendar-sync` | Anwendung | `root`, für den Dienst nur lesbar |
| `/etc/tanss-calendar-sync` | Konfiguration, Zertifikat | `0770`, Gruppe `tanss-sync` |
| `/var/lib/tanss-calendar-sync` | Zustandsdatenbank `state.db`, Token | `tanss-sync` |
| `/var/log/tanss-calendar-sync` | Protokolldateien | `tanss-sync` |
| `/run/tanss-calendar-sync` | Sperrdatei, bei jedem Reboot neu | `tanss-sync` |

Das Konfigurationsverzeichnis ist **gruppenschreibbar**, und das ist Absicht: Der Dienst
schreibt seine eigene Konfiguration, wenn Benutzer aktiviert werden oder der
Verzeichnisabgleich neue Mitarbeiter aufnimmt. Er ersetzt sie dabei über eine temporäre
Datei daneben — das braucht Schreibrecht am Verzeichnis, nicht nur an der Datei.

Der Preis gehört dazugesagt: Wer im Verzeichnis schreiben darf, darf dort auch umbenennen
und löschen — der Dienstbenutzer kann also auch `graph-secret` und `graph.pem` austauschen.
Das ist hinnehmbar, weil er sie ohnehin liest; es heißt aber, dass dieses Konto genauso zu
behandeln ist wie die Zugangsdaten selbst.

Das Skript ist **idempotent**: Zum Aktualisieren einfach erneut ausführen. Konfiguration,
Token und Zustandsdatenbank bleiben dabei unangetastet.

### Neustartfestigkeit

Sechs Dinge sorgen dafür, dass der Dienst einen Reboot und Störungen übersteht:

1. **`WantedBy=multi-user.target`** in Verbindung mit `systemctl enable` — der Dienst wird
   bei jedem Systemstart automatisch mitgestartet.
2. **`After=network-online.target` / `Wants=network-online.target`** — der Start wartet, bis
   das Netzwerk tatsächlich nutzbar ist, nicht nur bis ein Interface konfiguriert wurde.
3. **`Restart=always` mit `RestartSec=15`** — nach jedem Absturz oder Beenden startet der
   Dienst neu. `StartLimitBurst=5` innerhalb von 300 Sekunden verhindert dabei eine
   Endlosschleife bei dauerhaften Fehlern.
4. **`RuntimeDirectory=tanss-calendar-sync`** — `ProtectSystem=strict` macht die gesamte
   Dateisystemhierarchie schreibgeschützt, `/run` eingeschlossen. Ohne diesen Eintrag kann
   der Dienst seine Sperrdatei nicht anlegen und stirbt nach jedem Reboot sofort wieder.
   systemd legt das Verzeichnis beim Start selbst an und räumt es beim Stoppen ab.
5. **`RuntimeDirectoryPreserve=yes`** — ohne das räumt systemd das Verzeichnis beim
   Stoppen wieder ab, und ein Befehl von Hand als Dienstbenutzer scheitert daran, dass
   `/run` nur root beschreiben darf. Nach einem Reboot ist es trotzdem leer, `/run` ist
   ein tmpfs.
6. **`/etc/tmpfiles.d/tanss-calendar-sync.conf`** — legt das Verzeichnis beim Systemstart
   an, bevor der Dienst je gelaufen ist. Ohne den Eintrag entstünde es beim ersten Befehl
   von Hand als `root`, und der Dienst käme anschließend nicht mehr an seine eigene
   Sperrdatei.

Prüfen lässt sich das so:

```bash
systemctl is-enabled tanss-calendar-sync   # muss "enabled" ausgeben
systemctl status tanss-calendar-sync
sudo reboot                                 # danach erneut den Status prüfen
```

Der Dienst nimmt seine Arbeit nach einem Neustart genau dort wieder auf, wo er aufgehört
hat: Zuordnungen und Änderungsmarken liegen in `state.db`, es findet kein Vollabgleich statt.

### Deinstallation

```bash
sudo systemctl disable --now tanss-calendar-sync
sudo rm /etc/systemd/system/tanss-calendar-sync.service
sudo rm -f /etc/tmpfiles.d/tanss-calendar-sync.conf
sudo systemctl daemon-reload
sudo rm -rf /opt/tanss-calendar-sync
# Konfiguration und Zustand bewusst separat, damit nichts versehentlich verloren geht:
sudo rm -rf /etc/tanss-calendar-sync /var/lib/tanss-calendar-sync
```

Das Entfernen des Dienstes verändert **keine** Termine — weder in TANSS noch in
Microsoft 365. Beide Kalender bleiben so, wie sie sind.

---

## Microsoft 365 vorbereiten

1. **App registrieren** — im Entra-Portal unter *App-Registrierungen* → *Neue Registrierung*.
   Ein Kontotyp „Nur Konten in diesem Organisationsverzeichnis" genügt, eine Redirect-URI
   wird nicht benötigt. Notieren Sie **Anwendungs-ID (Client)** und **Verzeichnis-ID (Mandant)**.

2. **Berechtigungen vergeben** — unter *API-Berechtigungen* → *Microsoft Graph* →
   **Anwendungsberechtigungen**:

   | Berechtigung | Wofür |
   |---|---|
   | `Calendars.ReadWrite` | Termine lesen und schreiben |
   | `User.Read.All` | Postfächer zu TANSS-Mitarbeitern auflösen, auch über Alias-Adressen |

   Anschließend **Administratorzustimmung erteilen**.

   > `User.Read.All` wird nur für die automatische Zuordnung benötigt. Wer alle Postfächer
   > von Hand in der Konfiguration hinterlegt und `user_discovery.mode` auf `off` setzt,
   > kommt mit `Calendars.ReadWrite` allein aus. Ohne `proxyAddresses` — also ohne
   > Zuordnung über Alias-Adressen — genügt auch das schmalere `User.ReadBasic.All`.

3. **Zertifikat hinterlegen** — unter *Zertifikate & Geheimnisse*. Ein Zertifikat ist einem
   Client-Secret vorzuziehen, weil es nicht nach zwölf oder 24 Monaten stillschweigend
   abläuft. Ein selbst signiertes Zertifikat genügt:

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 1095 \
     -keyout graph.key -out graph.crt -subj "/CN=tanss-calendar-sync"
   cat graph.key graph.crt > /etc/tanss-calendar-sync/graph.pem
   chmod 640 /etc/tanss-calendar-sync/graph.pem
   chown root:tanss-sync /etc/tanss-calendar-sync/graph.pem
   ```

   Laden Sie `graph.crt` in der App-Registrierung hoch und tragen Sie den angezeigten
   Fingerabdruck in die Konfiguration ein.

4. **Kalenderzugriff einschränken** — `Calendars.ReadWrite` als Anwendungsberechtigung gilt
   zunächst für **alle** Postfächer des Mandanten. Beschränken Sie den Zugriff auf die Postfächer,
   die tatsächlich synchronisiert werden sollen. In Exchange Online geschieht das über
   *RBAC for Applications*:

   ```powershell
   Connect-ExchangeOnline

   New-ServicePrincipal -AppId <Anwendungs-ID> -ObjectId <Objekt-ID des Dienstprinzipals> `
       -DisplayName "TANSS Calendar Sync"

   New-ManagementScope -Name "TANSS-Sync-Postfaecher" `
       -RecipientRestrictionFilter "MemberOfGroup -eq '<DN der Gruppe>'"

   New-ManagementRoleAssignment -App <Objekt-ID> `
       -Role "Application Calendars.ReadWrite" `
       -CustomResourceScope "TANSS-Sync-Postfaecher"
   ```

   > **Wichtig:** Entfernen Sie danach die mandantenweite Zustimmung in Entra ID. Solange
   > beide bestehen, addieren sich die Berechtigungen und der Zugriff bleibt unbeschränkt.

---

## TANSS vorbereiten

1. **Recht vergeben.** Der Mitarbeiter, unter dessen Kennung der Dienst arbeitet, braucht das
   Recht *„Administration: System API-Tokens für ext. Anbindungen und Schnittstellen
   erzeugen"*. Ohne dieses Recht antwortet die Ausstellungsroute mit HTTP 403, der Dienst
   kann sein Token nicht selbst erneuern und läuft nach Ablauf still aus. Es muss ein
   Mitarbeiter der **eigenen** Firma sein — `ownState` liefert zu einem Kundenkontakt dessen
   Firma als `defaultCompany`, und darauf liefen dann alle internen Termine.

2. **Token erzeugen.** In der TANSS-Administration unter *API-Konfiguration* ein Token für
   eine externe Anbindung abrufen und hinterlegen:

   ```bash
   sudo tee /var/lib/tanss-calendar-sync/token >/dev/null <<< 'Bearer <hier das Token einfuegen>'
   sudo chown tanss-sync:tanss-sync /var/lib/tanss-calendar-sync/token
   sudo chmod 600 /var/lib/tanss-calendar-sync/token
   ```

   Der Ort ist nicht beliebig: Der Dienst erneuert das Token selbst und ersetzt die Datei
   dabei über eine temporäre daneben. Das braucht Schreibrecht am **Verzeichnis**, und
   `/var/lib/tanss-calendar-sync` gehört ihm. `0600` ist kein Vorschlag — `tanss-sync doctor`
   beanstandet jedes Token, das andere Benutzer lesen können.

   Dieses Token wird **einmalig** benötigt. Danach erneuert der Dienst es selbstständig,
   lange bevor es abläuft.

3. **E-Mail-Adressen prüfen.** Nur Mitarbeiter mit gepflegter E-Mail-Adresse können einem
   Postfach zugeordnet werden.

---

## Einrichtung

```bash
sudo -u tanss-sync /opt/tanss-calendar-sync/.venv/bin/tanss-sync setup \
     --config /etc/tanss-calendar-sync/config.json
```

Der Assistent führt durch alle Schritte und **prüft jeden davon sofort gegen die echten
Systeme**, statt Eingaben nur entgegenzunehmen. Schlägt ein Schritt fehl, nennt er die
wahrscheinliche Ursache und den konkreten nächsten Handgriff.

| Schritt | Was geprüft wird |
|---|---|
| TANSS-Adresse | Erreichbarkeit und Version |
| TANSS-Token | Testabruf; zusätzlich ein Trockentest, ob die spätere Token-Erneuerung funktionieren wird |
| Eigene Firma | wird automatisch ermittelt und nur zur Bestätigung angezeigt |
| Microsoft-App | Token-Abruf mit den hinterlegten Daten |
| Postfachzugriff | Testzugriff auf ein konkretes Postfach |
| Benutzerzuordnung | Abgleich der TANSS-Mitarbeiter mit den Postfächern, Tabelle zur Bestätigung |
| Bestehende Terminsynchronisation | Warnung, falls bereits eine andere Terminsynchronisation für einen Benutzer aktiv ist |
| Webhooks | Anlage der Ereignisregeln in TANSS |
| Probelauf | ein Durchlauf über sieben Tage ohne Schreibzugriff, mit Änderungsliste |
| Dienst | systemd-Unit aktivieren |

Der Assistent ist **wiederaufnehmbar**. Bestätigte Schritte werden zwischengespeichert; ein
Abbruch, auch mit `Ctrl-C`, verliert nichts.

Wird `tanss-sync` ohne vorhandene Konfiguration aufgerufen, startet der Assistent von selbst.

---

## Konfiguration

Die Konfiguration liegt als JSON-Datei unter `/etc/tanss-calendar-sync/config.json`.
Eine vollständige Vorlage findet sich in [`config.example.json`](config.example.json).

Geheimnisse stehen **nicht** in der Datei, sondern werden referenziert:

| Schreibweise | Bedeutung |
|---|---|
| `file:/pfad/zur/datei` | Inhalt der Datei wird gelesen (empfohlen) |
| `env:NAME` | Wert aus der Umgebungsvariable |
| `keyring:dienst/schlüssel` | Wert aus dem System-Schlüsselbund |

### `tanss`

| Parameter | Standard | Bedeutung |
|---|---|---|
| `base_url` | — | Basisadresse der TANSS-API, z. B. `https://tanss.example.com/backend`. Ohne abschließenden Schrägstrich. |
| `token_ref` | `file:/var/lib/tanss-calendar-sync/token` | Verweis auf das API-Token. Der Dienst schreibt hierhin auch das erneuerte Token. Beschreibbar sein muss dabei das **Verzeichnis**, nicht nur die Datei: Die Erneuerung legt eine temporäre Datei daneben und benennt sie um, damit nie ein halb geschriebenes Token entsteht. Deshalb liegt es unter `/var/lib/tanss-calendar-sync`, das dem Dienstbenutzer gehört. Ein `chmod` auf die Datei allein genügt nicht. |
| `token_owner_employee_id` | — | Mitarbeiter-ID, unter der die Token-Erneuerung erfolgt. Dieser Mitarbeiter braucht das Recht zum Erzeugen von API-Tokens und muss aktiv bleiben. |
| `rotate_before_days` | `60` | Ab welcher Restlaufzeit das Token erneuert wird. Der großzügige Vorlauf sorgt dafür, dass ein Problem lange vor dem Ablauf auffällt. |
| `own_company_id` | wird ermittelt | Die eigene Firma. Termine ohne Kundenbezug werden ihr zugeordnet, da in TANSS jeder Termin eine Firma braucht. |
| `verify_tls` | `true` | TLS-Zertifikat prüfen. Nur für Testumgebungen mit selbst signiertem Zertifikat abschalten. |
| `timeout_seconds` | `30` | Zeitüberschreitung für einzelne API-Aufrufe. |

### `microsoft`

| Parameter | Standard | Bedeutung |
|---|---|---|
| `tenant_id` | — | Verzeichnis-ID (Mandant) aus der App-Registrierung. |
| `client_id` | — | Anwendungs-ID (Client). |
| `auth.mode` | `certificate` | `certificate` oder `secret`. Zertifikat ist vorzuziehen, weil es nicht unbemerkt abläuft. |
| `auth.certificate_path` | — | Pfad zur PEM-Datei mit privatem Schlüssel und Zertifikat. |
| `auth.thumbprint` | — | Fingerabdruck des Zertifikats, wie im Portal angezeigt. |
| `auth.client_secret_ref` | `null` | Verweis auf das Client-Secret, falls `mode` auf `secret` steht. |
| `timezone` | `W. Europe Standard Time` | Zeitzone, in der Termine geschrieben werden. Windows-Schreibweise. |
| `timeout_seconds` | `30` | Zeitüberschreitung für Graph-Aufrufe. |

### `sync`

| Parameter | Standard | Bedeutung |
|---|---|---|
| `mode` | `poll` | `poll` fragt beide Seiten regelmäßig ab und braucht **keine** eingehende Freigabe. `push` lässt TANSS zusätzlich Ereignisse melden und reagiert schneller, erfordert aber einen erreichbaren Endpunkt (siehe `webhook`). |
| `interval_seconds` | `120` | Abstand zwischen zwei Durchläufen. Kleinere Werte erhöhen die Last auf beiden Seiten spürbar. |
| `window_days_past` | `14` | Wie weit in die Vergangenheit abgeglichen wird. Alles davor wird nie angefasst. |
| `window_days_future` | `180` | Wie weit in die Zukunft abgeglichen wird. |
| `conflict_winner` | `tanss` | Wer gewinnt, wenn beide Seiten seit dem letzten Abgleich geändert wurden: `tanss`, `outlook` oder `newest`. Dauer und Rundung entscheidet **immer** TANSS, unabhängig von dieser Einstellung. |
| `echo_suppression_seconds` | `30` | Wie lange nach einem eigenen Schreibvorgang die Rückmeldung der Gegenseite als eigenes Echo verworfen wird. Verhindert Endlosschleifen. |
| `company_suffix_in_subject` | `true` | Hängt den Firmennamen in Klammern an den Betreff im Outlook-Termin an, damit dort erkennbar ist, zu welchem Kunden er gehört. |
| `travel_time_as_separate_events` | `true` | Erzeugt für An- und Abfahrt jeweils einen eigenen Outlook-Termin. Die Blöcke heißen *Anfahrt* und *Abfahrt* und tragen die Kundenadresse im Ortsfeld. Bei `false` werden Fahrtzeiten nicht übertragen; bereits vorhandene Blöcke bleiben dann unangetastet, statt gelöscht zu werden. |
| `sync_series` | `true` | Bezieht Terminserien in den Abgleich ein. Serientermine werden **gepaart und geändert, aber nie neu angelegt**: In TANSS haben sie keinen eigenen Datensatz, in Outlook existieren sie nur als Teil ihrer Serie. |
| `infinite_series_end_years` | `2` | Enddatum, das eine endlose Outlook-Serie beim Übertragen nach TANSS bekommt, gerechnet ab ihrem Beginn. TANSS lehnt Serien ohne Ende ab. Der Wert wird bei jedem Lauf nachgezogen, die Serie wirkt dadurch weiterhin endlos. |
| `adopt_existing_days` | `90` | Wie weit rückwirkend der Bestand übernommen wird, wenn ein Benutzer aktiviert wird. Termine, die auf der Gegenseite bereits existieren, werden dabei **übernommen statt verdoppelt**; nur was dort wirklich fehlt, wird angelegt. Bei `0` bleibt der gesamte Bestand unangetastet — er wird dann allerdings auch nicht mehr gepflegt: Eine spätere Änderung an einem älteren Termin bliebe auf ihrer Seite liegen. |
| `sync_absences` | `true` | Überträgt Abwesenheiten nach Outlook. Immer nur in diese Richtung. |
| `ignore_show_as_free` | `true` | Übergeht Outlook-Termine mit Status *Frei* / *Verfügbar*. Abschalten führt dazu, dass auch geblockte Zeiten in TANSS landen. |
| `ignore_all_day` | `true` | Übergeht Outlook-Termine mit gesetztem Haken *Ganztägig*. |
| `dry_run` | `false` | Bei `true` wird nichts geschrieben, nur protokolliert, was geschehen würde. |

### `safety`

Diese Einstellungen schützen vor Massenlöschungen.

Die **Mengenbremsen stehen ab Werk auf `0`, also aus**. Sie hielten den Dienst bei gewöhnlichen Vorgängen an — einem zurückgezogenen Sammelurlaub, einem ersten Abgleich gegen einen gewachsenen Kalender —, und ein Alarm, der im Normalbetrieb schrillt, wird abgeschaltet statt beachtet. Wer sie will, setzt einen Wert größer `0`.

Unabhängig davon trägt weiterhin: Gelöscht wird ausschließlich, wenn der Termin gezielt nachgefragt wurde und dabei nachweislich nicht mehr existiert (HTTP 404) — das bloße Fehlen in einer Antwort genügt nie. Davor läuft eine Karenzzeit, und **vor jeder Löschung wird gesichert**; `tanss-sync deleted` zeigt die Sicherungen, `tanss-sync restore` holt einen Termin zurück, auf beiden Seiten. Abwesenheiten und Fahrt-Blöcke sind TANSS-seitig ohnehin schreibgeschützt.

| Parameter | Standard | Bedeutung |
|---|---|---|
| `max_creates_per_run` | `0` | Mehr Neuanlagen in einem Durchlauf brechen den Lauf ab. `0` (Standard) hebt die Grenze auf. |
| `max_deletes_per_run` | `0` | Mehr Löschungen in einem Durchlauf brechen den Lauf ab, ohne etwas zu schreiben. `0` (Standard) hebt die Grenze auf. |
| `max_delete_ratio` | `0` | Zusätzliche Grenze als Anteil der verknüpften Termine eines Benutzers. `0` (Standard) schaltet die Anteilsprüfung ab. |
| `ratio_floor` | `20` | Ab wie vielen verknüpften Terminen die Anteilsgrenze überhaupt gilt. Darunter sagt ein Anteil nichts: Bei zwei gekoppelten Terminen sind zwei Löschungen zwangsläufig 100 %, bei einem einzigen ist es jede Löschung. Ein Alarm, der bei jedem gewöhnlichen Vorgang schrillt, wird abgeschaltet und schützt dann gar nichts mehr. |
| `deletion_requires_probe` | `true` | Gelöscht wird nur bei einer tatsächlich beobachteten Löschung. Das bloße Fehlen in einer Antwort genügt nicht. **Lässt sich nicht abschalten** — die Konfiguration wird sonst zurückgewiesen. |
| `backup_retention_days` | `90` | Wie lange gelöschte Termine zur Wiederherstellung aufbewahrt werden. |

### `user_discovery`

| Parameter | Standard | Bedeutung |
|---|---|---|
| `mode` | `map_and_enable` | `map_and_enable` nimmt neue Mitarbeiter auf und synchronisiert sofort. `map_only` bereitet die Zuordnung vor, aktiviert aber nicht. `off` schaltet die automatische Erkennung ab. |
| `interval_minutes` | `60` | Abstand zwischen zwei Verzeichnisabgleichen. |
| `strict_matching` | `true` | Ordnet nur bei eindeutiger Übereinstimmung der E-Mail-Adresse zu. Bei `false` wird zusätzlich der Name herangezogen — bequemer, aber fehleranfälliger. |
| `auto_disable_on_removal` | `true` | Deaktiviert die Zuordnung, wenn ein Mitarbeiter oder Postfach verschwindet. **Termine werden dabei nie gelöscht.** |
| `mailbox_domains` | `[]` | Erlaubte Maildomänen. Wirkt als Sicherheitsnetz gegen Fehlzuordnungen. Leere Liste erlaubt alle. |

Neu aufgenommene Benutzer erhalten als Aktivierungszeitpunkt den Moment der Erkennung.
Ihre bereits bestehenden Termine bleiben dadurch unangetastet.

### `webhook`

Nur relevant bei `sync.mode` = `push`.

| Parameter | Standard | Bedeutung |
|---|---|---|
| `enabled` | `false` | Startet den Endpunkt, der Ereignismeldungen aus TANSS entgegennimmt. |
| `bind` | `127.0.0.1` | Adresse, auf der gelauscht wird. Muss von der TANSS-Instanz erreichbar sein. |
| `port` | `8787` | Port des Endpunkts. |
| `path_token` | — | Zufallszeichenkette im Pfad. Da die Meldungen keine eigene Authentifizierung mitbringen, ist dies die einzige Absicherung — der Endpunkt gehört ausschließlich ins interne Netz. |
| `callback_base_url` | — | Adresse, unter der TANSS den Endpunkt erreicht. Wird beim Anlegen der Ereignisregeln eingetragen. |

### `logging`

| Parameter | Standard | Bedeutung |
|---|---|---|
| `level` | `info` | `debug`, `info`, `warning` oder `error`. |
| `format` | `text` | `text` für Menschen, `json` für die Weiterverarbeitung. |
| `file` | `/var/log/tanss-calendar-sync/sync.log` | Zusätzlich zur Ausgabe an journald. |
| `rotate_mb` | `20` | Größe, ab der die Protokolldatei rotiert wird. |
| `keep_files` | `10` | Anzahl aufbewahrter Dateien. |
| `audit_retention_days` | `365` | Aufbewahrung des Änderungsprotokolls. |
| `operational_retention_days` | `30` | Aufbewahrung des Betriebsprotokolls. |
| `redact_content` | `false` | Ersetzt Betreff und Beschreibung im Protokoll durch eine Prüfsumme. Damit bleibt nachvollziehbar, *dass* sich etwas geändert hat, ohne den Inhalt zu speichern. Empfohlen bei strengen Datenschutzvorgaben. |
| `log_http` | `false` | Protokolliert jeden API-Aufruf. Hilfreich zur Fehlersuche, sonst sehr gesprächig. |

### `users`

Wird vom Assistenten und von der automatischen Erkennung gepflegt, kann aber von Hand
angepasst werden.

| Parameter | Bedeutung |
|---|---|
| `tanss_employee_id` | Mitarbeiter-ID in TANSS. |
| `tanss_email` | E-Mail-Adresse aus TANSS, dient der Zuordnung. |
| `mailbox` | Postfach in Microsoft 365. Weicht in Einzelfällen von `tanss_email` ab. |
| `enabled` | Ob dieser Benutzer synchronisiert wird. |
| `direction` | `both`, `tanss_to_m365` oder `m365_to_tanss`. |
| `activated_at` | Zeitpunkt der Aktivierung. Termine, die davor angelegt wurden, werden nie angefasst. |

---

## Kommandos

```
tanss-sync setup                      Geführte Ersteinrichtung
tanss-sync doctor                     Diagnose aller Voraussetzungen

tanss-sync sync --once                Ein Durchlauf
tanss-sync sync --once --dry-run      Zeigt geplante Änderungen, schreibt nichts
tanss-sync run                        Dauerbetrieb (wird vom Dienst verwendet)
tanss-sync status                     Letzter Lauf, Fehler, Warteschlange

tanss-sync health                     Kurzstatus mit Exit-Code für die Überwachung
tanss-sync ack                        Not-Aus quittieren (hebt die Sperre nicht auf)

tanss-sync users list                 Mitarbeiter, Postfächer und Status
tanss-sync users discover             Verzeichnisabgleich sofort ausführen
tanss-sync users enable <id>          Benutzer aktivieren
tanss-sync users disable <id>         Benutzer deaktivieren (löscht keine Termine)

tanss-sync token status               Restlaufzeit und nächster Erneuerungstermin
tanss-sync token rotate               Erneuerung sofort auslösen
tanss-sync token check-rotation       Trockentest der Erneuerungsfähigkeit

tanss-sync webhooks list              Ereignisregeln anzeigen, fremde markiert
tanss-sync webhooks check-tanssx      Läuft eine fremde Terminsynchronisation?
tanss-sync webhooks export <datei>    Regeln sichern (mit Rückrufadressen)
tanss-sync webhooks import <datei>    Gesicherte Regeln wieder anlegen
tanss-sync webhooks detach --user <id>   Regeln eines Mitarbeiters entfernen,
                                         vorher automatisch sichern
tanss-sync webhooks cleanup           Überzählige Regeln entfernen
tanss-sync webhooks sync              Eigene Regeln abgleichen (nur bei mode "push")

tanss-sync history --support <id>     Vollständige Historie eines Termins
tanss-sync history --user <id>        Historie eines Benutzers
tanss-sync deleted                    Was wurde wann und warum gelöscht
tanss-sync restore <sicherung>        Gelöschten Termin wiederherstellen
tanss-sync unlink --user <id>         Kopplung lösen, beide Seiten unverändert lassen
tanss-sync export-state               Alle Verknüpfungen als JSON

tanss-sync db check                   Integrität der Zustandsdatenbank prüfen
tanss-sync db backup <pfad>           Konsistente Sicherung ziehen
tanss-sync db vacuum                  Zustandsdatenbank verdichten
```

Jedes Kommando ist entweder lesend oder schreibend; `--help` sagt es bei jedem dazu.
Schreibende Kommandos nehmen eine Prozesssperre und brechen ab, solange ein anderer
Lauf sie hält.

`--dry-run` sollte vor jeder Inbetriebnahme und nach jeder Konfigurationsänderung verwendet
werden. Es ist die einzige Möglichkeit, die Auswirkungen einer Einstellung zu sehen, bevor
sie echte Kalender verändert.

---

## Betrieb

```bash
systemctl status tanss-calendar-sync        # Zustand
systemctl restart tanss-calendar-sync       # Neustart
journalctl -u tanss-calendar-sync -f        # Protokoll verfolgen
journalctl -u tanss-calendar-sync --since today
```

Nach einer Konfigurationsänderung:

```bash
sudo systemctl restart tanss-calendar-sync
```

**Empfohlene Inbetriebnahme:** Zunächst einen einzelnen Testbenutzer aktivieren, einige Tage
beobachten und die Historie stichprobenartig prüfen. Erst danach die übrigen Benutzer
aktivieren. Ein bidirektionaler Abgleich verändert echte Kalender — ein vorsichtiger Start
kostet wenig und erspart viel.

---

## Protokollierung

Es gibt drei Protokollebenen mit unterschiedlichem Zweck:

| Ebene | Zweck | Aufbewahrung |
|---|---|---|
| Betriebsprotokoll | Laufender Betrieb, an journald und als Datei | 30 Tage |
| Änderungsprotokoll | Jede schreibende Operation mit Vorher/Nachher und Begründung | 1 Jahr |
| Laufprotokoll | Kennzahlen je Durchlauf: Dauer, Anzahl, Fehler | 1 Jahr |

Entscheidend ist das Änderungsprotokoll: Es hält nicht nur fest, *dass* etwas passiert ist,
sondern **warum** — welche Felder sich geändert haben, was den Abgleich ausgelöst hat und
wie entschieden wurde. Damit lässt sich die Geschichte eines einzelnen Termins über beide
Systeme hinweg nachvollziehen:

```bash
tanss-sync history --support 57298
tanss-sync history --user 42 --since 7d
```

Tokens, Geheimnisse und Zertifikate werden **niemals** protokolliert. Termininhalte sind
personenbezogene Daten; gespeichert werden deshalb nur die geänderten Felder, und mit
`redact_content` lässt sich auch das auf Prüfsummen reduzieren.

---

## FAQ

### Warum bekommen Kunden Einladungs- oder Absagemails?

Microsoft 365 verschickt automatisch eine Einladung an alle Teilnehmer, sobald ein Termin
mit Teilnehmern angelegt oder geändert wird — und eine Absage, wenn er im Postfach des
Organisators gelöscht wird. Dieses Verhalten ist Teil von Exchange Online und lässt sich
weder abschalten noch unterdrücken. Kein Werkzeug, das über Microsoft Graph schreibt, kann
das ändern.

Was dieses Werkzeug tut, um unnötige Mails zu vermeiden:

- Termine, die aus TANSS stammen, werden **ohne Teilnehmerliste** angelegt. Ohne Teilnehmer
  gibt es niemanden, der benachrichtigt werden könnte.
- Teilnehmerlisten werden nur angefasst, wenn sie sich tatsächlich geändert haben. Ein
  Termin, bei dem sich nur die Uhrzeit ändert, löst keine Aktualisierung der Teilnehmer aus.
- Beim Löschen wird unterschieden, ob das betroffene Postfach der Organisator ist. Nur dort
  entsteht überhaupt eine Absagemail.

Wenn Sie Termine mit externen Teilnehmern führen und deren Benachrichtigung vermeiden
wollen, legen Sie diese Termine direkt in Outlook an und laden Sie die Teilnehmer dort ein.

### Warum wird die Dauer meiner Termine verändert?

TANSS rechnet Leistungen und Termine in Arbeitseinheiten und wendet dabei Rundungsregeln an.
Diese Regeln gelten auch für Termine, die aus Outlook kommen. Ein in Outlook eingetragener
Zehn-Minuten-Termin erscheint nach dem Abgleich in beiden Systemen mit der gerundeten Dauer.

Eine abweichende Dauer in beiden Systemen ist nicht möglich — es handelt sich um denselben
Termin. Wenn die Rundung stört, sollten die Rundungsparameter in TANSS angepasst werden.

### Wie bekomme ich einen ganztägigen Termin synchronisiert?

Nicht über den Haken **Ganztägig**. Outlook markiert solche Termine standardmäßig als
*Frei*, und freie Zeiten werden bewusst übergangen. Auch ein manuelles Zurücksetzen auf
*Gebucht* ändert daran nichts.

Tragen Sie stattdessen einen Termin von 00:00 bis 24:00 Uhr ein, **ohne** den Haken zu
setzen. Dieser wird normal übertragen.

### Warum werden freie Termine nicht übertragen?

Das ist Absicht und nützlich: Es gibt Ihnen eine einfache Möglichkeit, Einträge im Kalender
zu führen, die den Abgleich nichts angehen — Erinnerungen, Blocker, private Notizen. Setzen
Sie den Status auf *Frei* bzw. *Verfügbar*, und der Termin bleibt ausschließlich in Outlook.

Über `sync.ignore_show_as_free` lässt sich das abschalten, dann landen aber auch alle
Blocker in TANSS.

### Was passiert, wenn ich einen Termin unter Vorbehalt annehme?

TANSS kennt nur *angenommen* oder *abgelehnt*. Eine Zusage unter Vorbehalt wird deshalb als
vollwertige Zusage übertragen. Durch den Rückabgleich wird der Termin anschließend auch in
Outlook als angenommen geführt.

### Was passiert mit dem Outlook-Termin, wenn ich den Termin in TANSS in eine Leistung wandle?

Der Outlook-Termin bleibt unverändert bestehen. Die Kopplung endet in diesem Moment;
spätere Änderungen an diesem Termin werden nicht mehr übertragen. Das ist gewollt: Der
Termin hat stattgefunden und ist dokumentiert, der Kalendereintrag soll als Beleg stehen
bleiben.

War der Datensatz dagegen schon eine Leistung, als der Dienst ihn zum ersten Mal sah, so
entsteht in Outlook gar nichts — **auch keine Fahrtblöcke**. Eine Fahrt ist die Projektion
ihres Haupttermins; erscheint der nie im Kalender, gehört auch die Fahrt nicht dorthin.
Andernfalls stünden Anfahrt und Abfahrt im Kalender und dazwischen fehlte der Termin, zu dem
sie gehören.

### Warum steht der Firmenname in Klammern im Betreff?

In TANSS gehört jeder Termin zu einem Kundendatensatz, in Outlook gibt es dieses Konzept
nicht. Damit auch im Outlook-Kalender erkennbar ist, zu welchem Kunden ein Termin gehört,
wird der Firmenname an den Betreff angehängt.

Beachten Sie: Runde Klammern werden dabei als Metadaten behandelt. Bereits vorhandene
Klammerinhalte im Betreff gehen verloren. Verzichten Sie in Terminbetreffs auf runde
Klammern, oder schalten Sie `sync.company_suffix_in_subject` ab.

### Warum erscheinen Vor-Ort-Termine als drei Einträge?

TANSS kennt An- und Abfahrtzeiten als Bestandteil eines Termins, Outlook nicht. Damit die
Fahrtzeiten sichtbar bleiben und der eigentliche Termin trotzdem als solcher erkennbar ist,
werden drei Einträge erzeugt: Anfahrt, Termin, Abfahrt.

Wird der Termin in TANSS verschoben, wandern die Fahrten automatisch mit. Wird er in Outlook
verschoben, müssen die Fahrteinträge von Hand mitverschoben werden. Über
`sync.travel_time_as_separate_events` lässt sich die Funktion abschalten.

**Warum die Abfahrt manchmal neben statt unter dem Termin steht:** Outlook ordnet seinen
Kalender in Halbstundenfeldern. Endet ein Termin nicht auf einer solchen Grenze — etwa um
12:45 —, so fällt die unmittelbar anschließende Abfahrt in dasselbe Feld wie der Termin, und
Outlook stellt beide nebeneinander dar statt untereinander. Die Anfahrt ist davon nie
betroffen, weil sie exakt mit dem Terminbeginn endet.

Das ist eine reine Darstellungsfrage: Die Zeiten sind korrekt und stoßen lückenlos
aneinander, nachprüfbar über die Termindetails. Wer die gestapelte Ansicht braucht, legt die
Termine in TANSS auf halbe Stunden. Ein künstlicher Abstand zwischen Termin und Abfahrt hilft
**nicht** — er verschiebt die Abfahrt nicht aus dem Rasterfeld heraus, verfälscht aber die
Zeiten.

### Kann ich das Werkzeug parallel zu einer anderen Terminsynchronisation betreiben?

Nein. Zwei Systeme, die dieselben Kalender abgleichen, schreiben gegeneinander und erzeugen
Duplikate. Das Werkzeug erkennt eine bereits eingerichtete Terminsynchronisation und
**verweigert die Aktivierung** betroffener Benutzer, bis die andere Anbindung für diesen
Benutzer abgeschaltet ist.

Für eine Umstellung empfiehlt sich dieser Weg: einen einzelnen Benutzer aus der bisherigen
Synchronisation herausnehmen, hier aktivieren, einige Tage beobachten, dann die übrigen
nachziehen.

### Werden alte Termine übertragen, wenn ich einen Benutzer aktiviere?

Nein. Jeder Benutzer bekommt bei der Aktivierung einen Zeitstempel. Termine, die davor
bestanden, werden nie angefasst — weder übertragen noch geändert noch gelöscht. Das gilt
auch für Mitarbeiter, die später automatisch dazukommen.

### Was passiert, wenn ich einen Benutzer entferne?

Der Abgleich für diesen Benutzer stoppt. **Seine Termine bleiben in beiden Systemen
vollständig bestehen.** Es gibt keinen Weg, auf dem das Entfernen eines Benutzers zu
Löschungen führt — weder beim Deaktivieren in der Konfiguration, noch beim Deaktivieren in
TANSS, noch beim Verlust des Postfachs.

### Kann der Abgleich versehentlich viele Termine löschen?

Dagegen stehen vier Sicherungen, und sie greifen in dieser Reihenfolge.

**Der Nachweis am einzelnen Termin.** Gelöscht wird nur, wenn der Termin gezielt
nachgefragt wurde und der Server dabei geantwortet hat, dass es ihn nicht mehr gibt
(HTTP 404). Das bloße Fehlen in einer Liste genügt nie — das kann auch ein verschobenes
Zeitfenster oder eine unvollständige Antwort sein. Ebenso wenig genügt eine leere
Antwort: Eine abgewiesene oder gestörte Verbindung wird als Fehler gemeldet und bricht
den Durchlauf ab, statt als „nicht mehr vorhanden" gelesen zu werden.

**Die Karenzzeit.** Ein erkannter Löschvorgang wird zunächst nur vorgemerkt. Taucht der
Termin innerhalb von `deletion_grace_seconds` wieder auf, wird die Vormerkung
zurückgenommen. TANSS entfernt beim Wandeln einer Terminvormerkung in einen festen
Termin kurzzeitig den alten Datensatz — ohne dieses Fenster wäre das ein Löschbefehl für
einen Termin, der weiterlebt.

**Die Sicherung.** Vor jeder Löschung wird der Termin vollständig gesichert.
`tanss-sync deleted` zeigt, was wann und warum entfernt wurde, `tanss-sync restore` legt
ihn wieder an — auf beiden Seiten.

**Der Schreibschutz.** Abwesenheiten und Fahrt-Blöcke werden TANSS-seitig nie durch den
Abgleich verändert oder gelöscht, egal was in Outlook mit ihnen geschieht.

Zusätzlich lassen sich mit `max_deletes_per_run` und `max_delete_ratio` Mengengrenzen
setzen, bei deren Überschreitung ein Durchlauf abbricht, ohne etwas zu schreiben. Diese
Grenzen sind **ab Werk nicht aktiv** — siehe [`safety`](#safety).

### Braucht der Server eine öffentliche IP-Adresse?

Nein. Im Standardbetrieb (`sync.mode` = `poll`) baut der Dienst ausschließlich ausgehende
Verbindungen auf. Eingehende Freigaben sind nicht nötig.

Der optionale Betrieb mit `push` benötigt einen Endpunkt, den die TANSS-Instanz erreichen
kann — das ist typischerweise eine interne Adresse, keine öffentliche.

### Wie oft wird abgeglichen?

Standardmäßig alle zwei Minuten. Änderungen sind also spätestens nach zwei Minuten auf der
Gegenseite sichtbar. Mit `sync.mode` = `push` reagiert der Dienst auf Änderungen in TANSS
nahezu sofort.

### Muss ich das TANSS-Token regelmäßig erneuern?

Nein. Es wird genau einmal bei der Einrichtung hinterlegt. Danach erneuert der Dienst es
selbstständig, standardmäßig 60 Tage vor Ablauf, und prüft das neue Token, bevor er es
übernimmt. Schlägt die Erneuerung fehl, bleibt das bisherige Token aktiv und der Dienst
weist täglich darauf hin.

Zwei Voraussetzungen, die beide erfüllt sein müssen — jede für sich lässt die Erneuerung
scheitern, und zwar erst Jahre später beim Ablauf des Tokens:

* Der unter `token_owner_employee_id` hinterlegte Mitarbeiter bleibt aktiv und behält das
  TANSS-Recht **„Administration: System API-Tokens für ext. Anbindungen und Schnittstellen
  erzeugen"**. Fehlt es, antwortet die Ausstellungsroute mit HTTP 403. Es muss ein
  Mitarbeiter der **eigenen** Firma sein: `ownState` liefert zu einem Kundenkontakt dessen
  Firma als `defaultCompany`, und darauf liefen dann alle internen Termine.
* Das Verzeichnis, in dem das Token liegt, ist für den Dienstbenutzer beschreibbar (siehe
  `tanss.token_ref`).

Beides prüft `tanss-sync doctor` — die Zeile „Token-Erneuerung" führt einen Trockentest
gegen die echte Gegenstelle aus, der kein brauchbares Token erzeugt.

### Was passiert bei einem Neustart des Servers?

Der Dienst startet automatisch mit und nimmt die Arbeit dort wieder auf, wo er aufgehört
hat. Zuordnungen und Änderungsmarken liegen in der Zustandsdatenbank, ein Vollabgleich
findet nicht statt.

---

## Lizenz

[MIT](LICENSE) — © 2026 ProNet Systems GmbH

TANSS ist ein Produkt der HUCK IT GmbH, Roßdorf (Amtsgericht Darmstadt, HRB 95700). Dieses
Projekt ist ein unabhängiges Werkzeug, steht in keiner Verbindung zur HUCK IT GmbH und wird von
ihr weder unterstützt noch geprüft. Marken gehören ihren jeweiligen Inhabern; die Nennung dient
allein dazu, zu sagen, wofür dieses Werkzeug gemacht ist.

Microsoft 365, Outlook und Microsoft Teams sind Marken der Microsoft Corporation; auch zu ihr
besteht keine Verbindung.
