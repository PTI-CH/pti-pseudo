# Asynchrone Aufträge — Entwurf

**Datum:** 2026-08-25
**Zyklus:** A von zwei. Zyklus B (PDF + OCR) setzt darauf auf und hat eine
eigene Spec.

---

## 1. Warum

Zwei Lücken, eine bestehende und eine kommende.

**Die bestehende:** `sync_threshold_bytes` steht seit Phase 1 in
`backend/config.py` mit dem Kommentar „Ab dieser Grösse nicht mehr synchron
verarbeiten (Gateway-Timeout)" — und wird **nirgends benutzt**. Wer heute ein
20-MB-Word-Dokument hochlädt, bekommt es synchron verarbeitet oder läuft in den
Timeout des Balancers. Auch das `Job`-Modell in `backend/vault/models.py`
existiert bereits, angelegt „ab Phase 2", und ist ungenutzt. Beide wurden
vorgesehen und nie verdrahtet.

**Die kommende:** Zyklus B bringt OCR für gescannte PDFs. Am 2026-08-25
gemessen: **rund 2 Sekunden je Seite**. Eine 30-seitige Akte sind gut 60
Sekunden — jenseits dessen, was ein HTTP-Request offen bleibt. Ohne Aufträge
scheitert Zyklus B an der ersten dicken Akte.

**Warum dieser Zyklus allein steht:** Aufträge nützen *allen* Formaten, sind
ohne PDF vollständig testbar und beheben sofort etwas Bestehendes. Sie
nebenbei in Zyklus B mitzuerfinden hiesse, zwei Teilsysteme gleichzeitig zu
entwerfen.

---

## 2. Geltungsbereich

**Enthalten:** Auftragstabelle, Hintergrundarbeiter, Statusabfrage, Abholen des
Ergebnisses, Aufräumen, beides in Oberfläche und externer API.

**Ausdrücklich nicht enthalten:**

- **Keine Warteschlange über Redis, kein Celery, kein RQ.** Eine
  Datenbanktabelle trägt für dieses Volumen. Vor allem aber: Auf diesem Knoten
  dürfen wir keine Dienste einrichten (siehe Abschnitt 4) — ein weiteres
  bewegliches Teil wäre eines, das niemand betreuen kann.
- **Keine Prioritäten, keine Wiederholung fehlgeschlagener Aufträge.** Beides
  ist leicht nachzurüsten, wenn es je gebraucht wird. Heute wäre es
  Vorratsbau.
- **Kein Fortschritt in Prozent für Formate ohne Seitenbegriff.** Ein
  Word-Dokument hat keine Seiten, die der Adapter kennt; dort gibt es nur
  „läuft".

---

## 3. Entscheide des Auftraggebers

Getroffen am 2026-08-25, mit Begründung, damit sie später nachvollziehbar
bleiben.

| Frage | Entscheid | Begründung |
|---|---|---|
| Wo lebt die Eingabe während der Auftrag läuft? | **Verschlüsselt in der Datenbank, nach Abschluss gelöscht** | Übersteht Neustarts; kein Auftrag geht verloren. Der Preis ist eine geänderte öffentliche Zusage (Abschnitt 9). |
| Werden alle Läufe zu Aufträgen? | **Nur oberhalb einer Grössenschwelle** | Ein Textfeld-Lauf ist heute in unter einer Sekunde fertig. Daraus einen Auftrag mit Fortschrittsanzeige zu machen wäre ein Rückschritt für die häufigste Nutzung. |
| Auch für die externe API? | **Ja** | Andere PTI-Anwendungen können sonst weiterhin keine grossen Dateien schicken — und genau dafür gibt es `/api/ext/v1`. |

---

## 4. Architektur — und warum es kaum eine Wahl gibt

Der Jelastic-Runner startet **genau einen Befehl**:
`python -m uvicorn backend.main:app --workers 2`. Am 2026-08-25 auf dem Knoten
nachgesehen: Benutzer `python` (uid 700), **kein `sudo`**. Wir können also
weder einen systemd-Dienst anlegen noch einen zweiten Prozess unter Aufsicht
stellen.

Daraus folgt: **Der Hintergrundarbeiter lebt in der Anwendung.** Je
gunicorn-Worker ein Faden, der im Lifespan startet und die Auftragstabelle
abfragt.

### Zwei Worker, ein Auftrag

Beide Worker greifen nach derselben Arbeit. Die Zuteilung geschieht deshalb in
der Datenbank, nicht im Prozess:

```sql
SELECT … FROM jobs WHERE status = 'wartend'
ORDER BY created_at LIMIT 1
FOR UPDATE SKIP LOCKED
```

**SQLite kennt `SKIP LOCKED` nicht**, und die Testsuite läuft darauf. Der
Baustein braucht deshalb beide Wege: `FOR UPDATE SKIP LOCKED` auf PostgreSQL,
ein bedingtes `UPDATE … WHERE status = 'wartend'` mit Prüfung der betroffenen
Zeilenzahl auf SQLite. Der Test *„zwei Arbeiter, ein Auftrag, genau eine
Verarbeitung"* ist der wichtigste dieses Zyklus — ohne ihn wird ein Dokument
doppelt verarbeitet und der Vault bekommt zwei Zuordnungen für denselben Namen.

### Nur auf PostgreSQL

Der Hintergrundarbeiter startet im Lifespan (`backend/main.py`) nur, wenn
`get_engine().dialect.name == "postgresql"` — dieselbe Bedingung prüft
`warteschlange.hintergrundverarbeitung_moeglich`. Grund ist nicht die
Datenbanklogik selbst (die beherrscht beide Dialekte, siehe oben), sondern
SQLite: Testbetrieb und die Ein-Knoten-Rückfallvariante (`JELASTIC_SETUP.md`
1b) teilen sich dort über eine `StaticPool` **eine einzige** zugrunde
liegende Verbindung — ein Faden, der daneben pollt, zerlegt deren
Transaktionszustand für alle anderen Sitzungen. Auf PostgreSQL holt sich
jede Sitzung dagegen ihre eigene Verbindung aus dem Pool; dort ist ein
zweiter Faden unproblematisch.

Läuft diese Installation nicht auf PostgreSQL, weist `warteschlange.lege_an`
eine Datei oberhalb der Grössenschwelle mit **503** ab, statt eine
Auftragszeile anzulegen, die nie jemand abarbeiten würde — das wäre sonst ein
Auftrag, der für immer auf `wartend` stünde, während die Oberfläche endlos
nachfragt, ohne dass irgendwo stünde, warum. Die Textrouten bleiben davon
unberührt, sie laufen immer synchron.

### Abfragetakt

Zwei Sekunden. Bei einem Auftrag alle paar Minuten ist eine Abfrage je Sekunde
Verschwendung, bei einer Minute Wartezeit merkt es niemand.

### Nebenläufigkeit begrenzen

**Höchstens ein Auftrag je Worker gleichzeitig**, also zwei im ganzen Knoten.
Nicht wegen der Datenbank, sondern wegen des Speichers: Zyklus B misst 1.1 GB
Spitze je OCR-Lauf. Diese Grenze gehört in den Code und nicht in die
Knotenkonfiguration — eine Schleife, die Speicher frisst, soll an unserer
Grenze scheitern und nicht auf der Rechnung erscheinen.

---

## 5. Datenmodell

`Job` in `backend/vault/models.py` existiert und wird erweitert.

| Spalte | Typ | Zweck |
|---|---|---|
| `eingabe_blob` | `LargeBinary` | die verschlüsselte Eingabe — **auf `NULL` gesetzt, sobald der Auftrag endet** |
| `dateiname` | `String(255)` | für Download und Anzeige |
| `parameter_json` | `JSONVariant` | die Wahl des Nutzers beim Anlegen: Kategorien, `modus` (umkehrbar/anonym), `ki_pruefung`, bei der Rückführung die `vault_id` |
| `ergebnis_blob` | `LargeBinary` | das fertige Dokument, bis `expires_at` |
| `fortschritt` | `Integer` | erledigte Einheiten (Seiten), `NULL` wo es keine gibt |
| `gesamt` | `Integer` | Einheiten insgesamt |
| `fehler` | `Text` | Klartext für den Menschen |
| `aktor` | `String(200)` | `benutzer:<username>` oder `apikey:<id>` — für die Sichtbarkeitsregel |
| `updated_at` | `DateTime(tz)` | Erkennung hängengebliebener Aufträge |

**Jede dieser Spalten gehört in `NACHZUZIEHENDE_SPALTEN` in `backend/db.py`.**
`create_all()` legt fehlende *Tabellen* an, aber keine fehlenden *Spalten* — die
neue Spalte entstünde in Produktion nicht von selbst und fiele erst beim ersten
Zugriff auf. Die Regel steht in CLAUDE.md und ist für genau diesen Fall dort.

`status` nimmt: `wartend`, `laeuft`, `fertig`, `fehler`.

**`parameter_json` enthält keine Inhalte** — nur die Wahl des Nutzers. Ein
Kategorienname ist keine Personenangabe; die `vault_id` ist eine Kennung, kein
Klartext. Damit gilt auch hier die Regel aus `backend/pipeline.py`: gespeichert
wird Zählbares und Gewähltes, nie Text aus dem Dokument.

**Die Fundauswahl gehört dazu** — anders als zunächst angenommen. Beim
Planen nachgesehen: `POST /documents:anonymize-file` nimmt ein Feld
`selected_entities`, und die Oberfläche ruft die Route beim Druck auf
„Anwenden" ein zweites Mal auf, diesmal mit den abgewählten Funden. Dieser
zweite Aufruf ist ein vollwertiger Dateilauf derselben Grösse — er wird also
ebenfalls ein Auftrag, und `parameter_json` muss `selected_entities` mitführen.

Wäre das übersehen worden, hätte „Anwenden" bei grossen Dateien stillschweigend
**alle** Funde ersetzt statt der gewählten.

**Nur das Tripel wandert mit, nie der Klartext.** `backend/pipeline._select`
identifiziert einen Fund ausschliesslich über `(segment_id, start, end)` —
nichts sonst wird gebraucht. Die Oberfläche schickt aus historischen Gründen
je Fund zusätzlich `text` (den im Original gefundenen Klartext) und `score`
mit; ein Aufrufer über `/api/ext/v1` könnte das ebenso tun. Würde `lege_an`
das Objekt ungefiltert übernehmen, stünde die vollständige Fundliste eines
Dokuments — Namen, AHV-Nummern, IBANs — lesbar in `parameter_json`, obwohl
diese Spalte laut Zusage oben keine Inhalte tragen soll. `warteschlange.
_ohne_klartext` streift deshalb vor dem Schreiben jedes Feld ausser
`segment_id`, `start`, `end` und `entity_type` ab — serverseitig und für
jeden Aufrufer, nicht nur in der Oberfläche.

### Verschlüsselung

Dieselbe Machart wie der Vault (`backend/vault/`): AES-256-GCM mit
`VAULT_MASTER_KEY`. Kein neuer Schlüssel, kein neues Verfahren — ein zweites
Krypto-Schema wäre eine zweite Stelle, an der etwas falsch sein kann.

**Je eine Spalte, keine getrennte Nonce.** Beim Planen nachgelesen:
`crypto.seal()` liefert bereits `Nonce || Ciphertext || Tag` in einem Blob. Ein
zweites Feld wäre nicht nur überflüssig, sondern eine Gelegenheit, Nonce und
Ciphertext auseinanderlaufen zu lassen.

`seal()` und `open()` arbeiten allerdings auf `str`. Für Dokumentbytes braucht
es ein Geschwisterpaar `seal_bytes()` / `open_bytes()` im selben Modul, das
`_derive()` mitbenutzt — nicht eine Umkodierung über Base64, die den
Speicherbedarf ohne Gegenwert um ein Drittel erhöhte.

**Das Ergebnis wird ebenfalls verschlüsselt**, obwohl es pseudonymisiert ist.
Es kostet nichts und erspart die Frage, ob im Ergebnis nicht doch etwas
Übersehenes steht — und übersehen wird nachweislich etwas (Recall 100 % am
Golden-Set heisst nicht 100 % in freier Wildbahn).

---

## 6. Schnittstelle

### Anlegen

Betroffen sind die **Dateipfade**, nicht die Textpfade:

| Route | oberhalb der Schwelle |
|---|---|
| `POST /documents:anonymize-file` | **202** mit `job_id` |
| `POST /documents:reverse-file` | **202** mit `job_id` |
| `POST /documents:detect` · `:anonymize` · `:reverse` | unverändert synchron |

Die drei Textrouten nehmen JSON mit einem `text`-Feld und sind bereits durch
`max_text_chars` (300 000 Zeichen) begrenzt — sie laufen in Sekundenbruchteilen
und brauchen keinen Auftrag. Wären sie einbezogen, bekäme die häufigste Nutzung
des Werkzeugs ohne Not einen Umweg.

Die Rückführung gehört dazu, obwohl sie schnell ist: Ein 20-MB-Dokument
zurückzuführen heisst, es erneut zu zerlegen und zusammenzusetzen. Dieselbe
Grösse, dasselbe Timeout-Risiko.

Antwort oberhalb der Schwelle, **202 Accepted**:

```json
{ "job_id": "3f2a…", "status": "wartend" }
```

Die Schwelle ist `sync_threshold_bytes` (heute 2 MiB) — die vorhandene, bisher
ungenutzte Konstante. Welche Arbeit ein Auftrag ausführt, steht in der
bestehenden Spalte `mode` (`anonymize` oder `reverse`); sie wurde in Phase 1
genau dafür angelegt.

### Abfragen

`GET /jobs/{id}`

```json
{ "id": "3f2a…", "status": "laeuft", "fortschritt": 7, "gesamt": 30,
  "dateiname": "protokoll.docx", "fehler": null, "erstellt": "…",
  "laeuft_ab": "…" }
```

Enthält **keine Inhalte** — weder Fundtexte noch Auszüge. Dieselbe Regel wie
beim Protokoll (`backend/pipeline.py`: „Protokolliert wird ausschliesslich
Zählbares").

### Abholen

`GET /jobs/{id}/result` — liefert das Dokument. Nur bei `status = "fertig"`,
sonst **409 Conflict** mit dem aktuellen Status im Rumpf.

### Sichtbarkeit

**Ein Aufrufer sieht ausschliesslich seine eigenen Aufträge.** Geprüft wird
gegen `aktor`, nicht nur gegen `tenant_id`: Ein API-Schlüssel darf die Aufträge
eines anderen Schlüssels nicht sehen und erst recht nicht die eines Menschen in
der Oberfläche. Fremder oder unbekannter Auftrag → **404**, nicht 403; ein 403
verriete, dass es ihn gibt.

Der Router wird unter `/api/v1` **und** `/api/ext/v1` eingehängt und gehört
damit in `DUAL_MOUNT_ROUTERS` in `backend/main.py`, mit
`dependencies=[Depends(require_api_key)]` am ext-Pfad.

---

## 7. Fehlerpfade

Die drei, die im Betrieb wirklich auftreten:

**1. Neustart mitten im Auftrag.** Ein Deploy dauert 3–6 Minuten und trifft
jeden laufenden Auftrag. Ohne Gegenmassnahme bliebe er für immer auf `laeuft`
stehen. Ein Auftrag, dessen `updated_at` älter als **10 Minuten** ist, gilt als
verwaist und geht zurück auf `wartend`. Die Frist ist grosszügig gegenüber
einem langsamen OCR-Lauf und knapp genug, dass niemand ewig wartet.

Ein Auftrag, der so **dreimal** freigegeben wurde, geht auf `fehler` — sonst
wandert ein Dokument, das den Arbeiter zuverlässig abstürzen lässt, für immer
im Kreis.

**2. Die Verarbeitung scheitert.** Status `fehler`, Meldung im Klartext für den
Menschen. **Die Eingabe wird trotzdem gelöscht** — auch der gescheiterte
Auftrag darf keine Personendaten liegenlassen. Das ist eine eigene Zusicherung
und bekommt einen eigenen Test.

**3. Niemand holt das Ergebnis ab.** `expires_at` steht auf **24 Stunden** nach
Fertigstellung. Ein Aufräumlauf im selben Faden entfernt abgelaufene Zeilen
mitsamt Ergebnis. Vierundzwanzig Stunden decken „ich mache morgen früh weiter"
ab, ohne dass sich Dokumente über Wochen sammeln.

---

## 8. Oberfläche

Der Bereich „Pseudonymisieren" bekommt einen Wartezustand: Statuszeile,
Fortschritt wo vorhanden („Seite 7 von 30"), Abbruch nicht nötig — wer weggeht,
findet den Auftrag beim nächsten Laden wieder.

**Die Fundliste erscheint erst, wenn der Auftrag fertig ist.** Ein halb
gefülltes Ergebnis wäre gefährlicher als gar keines: Es sähe aus, als sei das
Dokument geprüft.

Abfragetakt in der Oberfläche: zwei Sekunden, dieselbe Grössenordnung wie im
Arbeiter.

---

## 9. Datenschutz und die öffentliche Aussage

Heute steht im Dossier (`landing`, §9):

> **📁 Keine Dauerspeicherung** — Dokumente werden nur flüchtig während der
> Verarbeitung gehalten und danach gelöscht.

Mit Aufträgen stimmt „flüchtig" nicht mehr: Die Eingabe liegt für die Dauer der
Verarbeitung in der Datenbank. Vorgeschlagener neuer Wortlaut, **zur
Entscheidung des Auftraggebers, nicht gesetzt**:

> **📁 Keine Dauerspeicherung** — Kleine Dokumente werden nur im Arbeitsspeicher
> verarbeitet. Grössere laufen als Auftrag; das Original liegt dabei
> AES-256-GCM-verschlüsselt in der Datenbank und wird **gelöscht, sobald der
> Auftrag endet** — auch wenn er scheitert. Das pseudonymisierte Ergebnis bleibt
> 24 Stunden abholbar und wird danach automatisch entfernt.

---

## 10. Abnahme

Fachlich abgenommen ist der Zyklus, wenn diese sechs Sätze durch Tests gedeckt
sind:

1. **Zwei Arbeiter, ein Auftrag, genau eine Verarbeitung.** Der wichtigste
   Test. Auf beiden Datenbanken (PostgreSQL-Weg und SQLite-Weg).
2. **Die Eingabe ist nach dem Ende weg** — bei Erfolg *und* bei Fehler.
3. **Unterhalb der Schwelle bleibt alles wie heute**: keine Auftragszeile,
   Antwort synchron. Die drei Textrouten bleiben in jedem Fall synchron.
4. **Ein verwaister Auftrag wird wieder aufgenommen**, und nach dreimal
   Verwaisen endgültig als Fehler markiert.
5. **Ein API-Schlüssel sieht fremde Aufträge nicht** — Antwort 404, nicht 403.
6. **Ein abgelaufener Auftrag ist samt Ergebnis verschwunden.**

Dazu die Hausregeln: `ruff check .` über das ganze Repo, das Golden-Set-Gate
unverändert grün, Frontend-Typprüfung und Build.

---

## 11. Offene Punkte

- **Fortschritt je Format.** Seiten gibt es bei PDF; bei docx/xlsx müsste man
  Segmente zählen. Für Zyklus A genügt „läuft"; Zyklus B bringt die Seitenzahl
  mit, wenn er sie braucht.
- **Aufräumen bei leerem Betrieb.** Der Aufräumlauf hängt am Arbeiterfaden.
  Läuft nie ein Auftrag, läuft er trotzdem — das ist Absicht, kostet aber eine
  Datenbankabfrage alle zwei Sekunden. Falls das stört: Intervall entkoppeln.
- **Die Schwelle von 2 MiB ist geerbt, nicht gemessen.** Sie stammt aus Phase 1
  und niemand hat je nachgesehen, ab welcher Grösse ein Lauf wirklich zu lange
  dauert. Nach dem ersten Betrieb nachmessen und begründet setzen.
