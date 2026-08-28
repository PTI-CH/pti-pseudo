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

**Nachtrag 2026-08-26 — „findet den Auftrag beim nächsten Laden wieder" ist
eingelöst.** Bis dahin lebte die `job_id` nur in der laufenden Anfrage; ein
Seitenneuladen verlor sie ersatzlos, und da es keine Liste eigener Aufträge
gibt, war der Auftrag danach für niemanden mehr auffindbar.
`frontend/src/core/auftragsspeicher.ts` merkt sie jetzt in `localStorage`
(Kennung, Art, Dateiname — keine Inhalte), `api.verfolgeAuftrag` merkt und
vergisst als einziger Weg, und beide Bereiche — „Pseudonymisieren" und
„Rückführen" — nehmen einen gemerkten Auftrag beim Start ohne Rückfrage
wieder auf.

---

## 9. Datenschutz und die öffentliche Aussage

Bis zum 2026-08-25 stand im Dossier (`landing`, §9):

> **📁 Keine Dauerspeicherung** — Dokumente werden nur flüchtig während der
> Verarbeitung gehalten und danach gelöscht.

Mit Aufträgen stimmte „flüchtig" nicht mehr: Die Eingabe liegt für die Dauer
der Verarbeitung in der Datenbank. **Der Auftraggeber hat den neuen Wortlaut
entschieden und er ist gesetzt** (`landing` #8, 2026-08-25):

> **📁 Keine Dauerspeicherung** — Kleine Dokumente werden nur im Arbeitsspeicher
> verarbeitet. Grössere laufen als Auftrag im Hintergrund; das Original liegt
> dabei AES-256-GCM-verschlüsselt in der Datenbank und wird **gelöscht, sobald
> der Auftrag endet** — auch wenn er scheitert. Das pseudonymisierte Ergebnis
> bleibt 24 Stunden abholbar und wird danach automatisch entfernt.

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

*Am 2026-08-28 abgearbeitet — was hier stand, steht jetzt als Befund da.*

### Fortschritt je Format — erledigt, aber nicht wie geplant

Ursprünglich stand hier: „Seiten gibt es bei PDF; bei docx/xlsx müsste man
Segmente zählen. Für Zyklus A genügt ‚läuft'." Der Satz war der Grund, warum
`melde_fortschritt` zwei Zyklen lang existierte, **ohne dass ihn jemals
jemand rief** — und `fortschritt`/`gesamt` damit für jeden Auftrag auf `null`
standen. „Für Zyklus A genügt ‚läuft'" hat sich als falsch erwiesen: Ein
zehn Minuten laufender Auftrag ohne Zähler ist von einem hängengebliebenen
nicht zu unterscheiden, und das ist genau der Zustand, gegen den dieser
ganze Zyklus gebaut wurde.

Gezählt wird jetzt das **Segment**, in beiden Auftragsarten. Die Wartezeit
auf eine Seitenzahl war ohnehin vergeblich: Seiten entstehen in docx und
xlsx erst beim Setzen und stehen gar nicht in der Datei. Das Segment ist
die Einheit, die der Adapter liefert und an der die Arbeit hängt.

Der Preis dafür steht in `CLAUDE.md` und in `API.md`: `fortschritt == gesamt`
heisst **nicht** „gleich fertig", weil nur der Erkennungsdurchlauf gezählt
wird und KI-Schicht, Übertragung und Zurückschreiben danach kommen. Das ist
bewusst so gelassen — der Nenner für die Nacharbeit wäre geschätzt, und ein
Balken, der bei 60 % hängt, weil jemand geraten hat, ist die schlechtere
Lüge.

### Aufräumen bei leerem Betrieb — erledigt, und der Befund war ein anderer

Der Punkt zielte auf die falsche Stelle. Nicht der Aufräumlauf kostete „eine
Datenbankabfrage alle zwei Sekunden", sondern die Warteschlangen-Abfrage;
der Aufräumlauf lief nur jeden 150. Durchgang. Beim Nachsehen fiel dafür das
eigentliche Problem auf: **„jeder 150. Durchgang" ist nicht „alle fünf
Minuten"**, weil ein Durchgang nur dann `TAKT_SEKUNDEN` wartet, wenn er
nichts gefunden hat. Unter Last zählte der Arbeiter seine 150 Durchgänge in
der Zeit, die 150 Aufträge brauchen — er räumte also am häufigsten auf, wenn
die Datenbank ohnehin zu tun hat.

Der Takt hängt jetzt an der monotonen Uhr (`AUFRAEUMEN_ALLE`, fünf Minuten).

**Die Abfrage im Leerlauf bleibt bei zwei Sekunden**, ausdrücklich. Sie zu
strecken war die naheliegende Sparmassnahme und ist die falsche: Das ist die
Zeit, die ein Mensch nach dem Hochladen wartet, bevor überhaupt etwas
beginnt. Eine Abfrage je Worker und Sekunde gegen einen Index ist der
günstigere Teil dieses Tauschs.

### Die Schwelle von 2 MiB — gemessen, und der Befund ist unbequem

`scripts/schwelle_messen.py` baut Dokumente wachsender Textmenge und lässt
sie durch `pipeline.anonymize_document` laufen (ohne KI-Schicht, anonymer
Modus). Lauf vom 2026-08-28, Entwicklungsmaschine:

| Format | Grösse | Zeichen | Segmente | Sekunden | s/1000 Z | Z/KiB | Auszug% |
|---|---|---|---|---|---|---|---|
| docx | 37K | 8'179 | 35 | 0.45 | 0.055 | 222 | 4.9 % |
| docx | 38K | 32'012 | 137 | 1.59 | 0.050 | 846 | 1.7 % |
| docx | 39K | 64'107 | 274 | 3.27 | 0.051 | 1'640 | 0.8 % |
| txt | 8K | 8'247 | 1 | 0.34 | 0.041 | 1'007 | 0.0 % |
| txt | 64K | 64'653 | 1 | 3.55 | 0.055 | 1'007 | 0.0 % |
| xlsx | 4K | 8'179 | 35 | 0.34 | 0.042 | 2'148 | 0.3 % |
| xlsx | 8K | 64'107 | 274 | 2.98 | 0.046 | 8'305 | 0.1 % |

**Vier Befunde, und der vierte ist der eigentliche.**

1. **Die Dauer ist linear im Text.** 0.030 bis 0.055 Sekunden je 1000
   Zeichen, über alle drei Formate und über den ganzen gemessenen Bereich —
   rund 20'000 Zeichen je Sekunde. Kein Knick, keine Superlinearität. Der
   Verdacht, `propagate_findings` könnte bei vielen Funden quadratisch
   werden, hat sich damit **nicht** bestätigt.
2. **Bei einem 60-Sekunden-Budget und dreifacher Sicherheit sind rund
   360'000 Zeichen tragbar.** Das ist die belastbare Hälfte der Rechnung.
3. **Bytes sind ein schlechter Stellvertreter für Text — schlechter als
   gedacht.** Dieselben 64'107 Zeichen ergeben 64 KiB als txt, 39 KiB als
   docx und **8 KiB** als xlsx: Faktor acht zwischen den Formaten. Und die
   Dichte *steigt* mit der Dokumentgrösse, weil sich die Formatgrundlast
   (bei docx rund 36 KiB Vorlage, Thema, Schriften) amortisiert.
4. **Daraus folgt: Eine Schwelle in Bytes hat keinen stabilen sicheren
   Wert.** Aus dem dichtesten gemessenen Punkt (8'305 Zeichen je KiB) fallen
   44 KiB heraus — und dieser Wert sinkt weiter, je grösser man misst. Die
   heutigen 2 MiB stehen dem gegenüber: Bei der Dichte eines xlsx sind das
   über 17 Millionen Zeichen, also **rund 15 Minuten** in einem Request, der
   nach einer Minute abgeschnitten wird. Die Schwelle schützt heute nicht,
   wovor sie schützen soll.

**Das Kriterium selbst ist der Fehler, nicht sein Wert.** Der Kopf von
`schwelle_messen.py` argumentierte zunächst, Bytes seien trotz allem richtig,
weil die Entscheidung fallen muss, *bevor* die Datei geöffnet wird — ein
Adapterlauf zum Abschätzen wäre selbst schon ein Teil der teuren Arbeit.
**Die Messung widerlegt das:** Der Adapterlauf kostet 0.0 bis 4.9 % der
Gesamtdauer, und der Anteil *fällt* mit der Dokumentgrösse. Genau bei den
Dateien, bei denen die Entscheidung zählt, ist das Öffnen also fast gratis.

**Entschieden am 2026-08-28: beide Schranken, in dieser Reihenfolge.**
Die Byte-Prüfung bleibt als grobe Vorprüfung, die Zeichenzahl nach dem
Adapterlauf entscheidet scharf (`documents._laeuft_als_auftrag`).

Die Reihenfolge ist tragend und nicht Geschmack. Die Byte-Prüfung *zuerst*,
weil sie die einzige ist, die auch bei einer **beschädigten** Datei greift:
`extract_segments` wirft daran, und der Auftragspfad ist genau der Ort, an
dem so ein Fehler sauber als gescheiterter Auftrag endet statt als `500`
mitten in einer Anfrage. Umgekehrt gilt: Sie allein reicht nicht, weil die
Dichte je Format um das Achtfache schwankt.

Der Wert der Zeichenschranke ist `300_000` und nicht die gemessenen 360'000
— das lässt Luft und fällt mit `max_text_chars` zusammen, sodass es im
ganzen Werkzeug **eine** Zahl dafür gibt, wie viel Text synchron verarbeitet
wird.

Verworfen wurden:

- **Nur die Byte-Schwelle senken** (etwa auf 256 KiB). Kleinster Eingriff,
  aber er behebt den Fall xlsx nicht: 256 KiB sind dort immer noch über zwei
  Millionen Zeichen. Und er trifft die falschen Dokumente — ein
  bildlastiges docx von 300 KiB ist in Sekunden durch und liefe ohne Not
  über den Auftragspfad.
- **So lassen.** Vertretbar nur unter der Annahme, dass im Betrieb keine
  dichten Dokumente vorkommen — und genau das ist ungeprüft.

**Nebenwirkung, die man kennen muss:** Die Ein-Knoten-Rückfallvariante
(`JELASTIC_SETUP.md` 1b, SQLite, kein Arbeiter) beantwortet jetzt auch
textreiche kleine Dateien mit `503` statt sie synchron durchlaufen zu
lassen. Das ist die richtige Antwort — dort wäre der Lauf ohnehin in den
Timeout gelaufen —, aber es sind mehr Dateien als vorher.

Was das Skript **nicht** klären kann, ist das Zeitbudget selbst: Wie lange
der NGINX-Balancer auf eine Antwort wartet, steht in seiner Konfiguration,
und der Jelastic-Token ist auf `Vcs.Update` + `RestartNodes` beschränkt.
60 Sekunden ist die NGINX-Vorgabe für `proxy_read_timeout` und die
plausibelste Annahme — aber eine Annahme. Ebenso gilt: Gemessen wurde auf
der Entwicklungsmaschine, nicht auf dem Jelastic-Knoten, und der synthetische
Text ist regelmässiger als echter, was die Dichte eher über- als
unterschätzt. Beide Vorbehalte ändern die Grössenordnung nicht.

---

## 12. Das Zeitbudget — GEMESSEN am 2026-08-28

> **Erledigt.** Der Balancer wartet **300 Sekunden**, nicht 60. Gemessen aus
> VS Code gegen die Produktion, mit einem auf sechs Stunden befristeten
> API-Schlüssel (inzwischen widerrufen) und synthetischen Dokumenten — also
> nach Variante 2 unten, **ohne** die Produktion umzukonfigurieren.
>
> | Zeichen | Ergebnis |
> |---|---|
> | 150'000 | `200` nach 81 s |
> | 200'000 | `200` nach 36 s |
> | 250'000 | `200` nach 57 s |
> | 270'000 | `200` nach 63 s / 124 s / 127 s (drei Läufe) |
> | 280'000 | **`504` nach 300.5 s** |
> | 280'000 | `200` nach 67 s (Wiederholung) |
>
> **Der `504` ist eine Zeit-, keine Grössengrenze.** Derselbe Umfang lief
> beim zweiten Versuch in 67 s durch. Was streut, ist die Verarbeitungsdauer
> — Faktor zwei bei identischer Eingabe, durch die Konkurrenz um die zwei
> uvicorn-Worker.
>
> **Der Durchsatz ist viel niedriger als angenommen:** rund 2'100–4'300
> Zeichen/s statt der hier gerechneten 20'000/s. Die beiden Fehler haben
> sich teilweise aufgehoben — das Budget ist fünfmal grösser, der Durchsatz
> rund fünfmal kleiner.
>
> **`SYNC_THRESHOLD_CHARS` wurde NICHT angehoben.** Bei dieser Streuung
> würde eine Schwelle, die den Mittelwert ausreizt, unter Last reissen; und
> gemessen wurde mit einem einzelnen Aufrufer. Nachgetragen sind die 300 s
> in `backend/config.py` und als `BUDGET_SEKUNDEN` in
> `scripts/schwelle_messen.py`. Wer die Schwelle anheben will, hat damit
> jetzt die Grundlage — als eigene Entscheidung mit eigener Lastmessung.
>
> Der Abschnitt darunter bleibt als Beleg stehen, wie die Frage entstand.

---

**Der einzige Punkt aus §11, der nicht geschlossen war** — bewusst
zurückgestellt am 2026-08-28, weil er aus einer Web-Session heraus nicht zu
klären war. Zu erledigen aus **VS Code**, also von einer Maschine mit
echtem Netzweg auf die Produktion.

### Was offen ist

Die ganze Auslegung der Schwelle hängt an einer Zahl, die niemand gemessen
hat: **wie lange der NGINX-Balancer auf eine Antwort wartet, bevor er den
Request abschneidet.** Überall im Baum steht dafür „eine Minute"; das ist
`proxy_read_timeout`s NGINX-Vorgabe und plausibel, aber nirgends
nachgeprüft. Ist der Wert in Wirklichkeit 30 Sekunden, ist
`SYNC_THRESHOLD_CHARS = 300_000` zu hoch; ist er 300 Sekunden, verschieben
wir Dokumente ohne Not in den Auftragspfad — und drängen dabei die
Ein-Knoten-Rückfallvariante unnötig in ihre 503.

### Warum es hier nicht ging

Zwei Wege wären naheliegend, beide sind aus dieser Umgebung versperrt:

* **Konfiguration lesen.** Der Jelastic-API-Token ist methoden-beschränkt
  auf `Vcs.Update` + `RestartNodes`; `execcmd` und Log-Lesen antworten mit
  `result 8202` (siehe CLAUDE.md, „Betriebs-Eigenheiten"). Über den
  SSH-Gate käme man an den Anwendungsknoten, **nicht** an den Balancer.
* **Von aussen provozieren.** Dazu braucht es eine echte Anfrage gegen
  `pseudo.pti-tip-hub.ch` mit gültiger Anmeldung — also eine Maschine mit
  Browser-Session oder API-Schlüssel.

### Vorschlag für die Messung

Gesucht ist die Wandzeit, bei der der Balancer aufgibt. Zwei Verfahren,
das erste ist das ehrlichere:

1. **Mit echten Dokumenten, über die Schwelle hinaus.** `SYNC_THRESHOLD_CHARS`
   im Dashboard vorübergehend sehr hoch setzen (etwa `10000000`), damit die
   Datei tatsächlich synchron läuft, dann `documents:anonymize-file` mit
   synthetischen Dokumenten wachsender Zeichenzahl aufrufen und die Wandzeit
   je Antwort messen. Bei rund 20'000 Zeichen/s entspricht 1 Mio. Zeichen
   etwa 50 Sekunden — die Grenze liegt also im Bereich, den man mit
   zwei, drei Läufen einkreist. Gesucht ist der Punkt, an dem statt `200`
   ein `502`/`504` oder ein Verbindungsabbruch kommt.
   **Danach die Variable zurücksetzen und den Knoten neu starten** (eine
   Dashboard-Änderung wirkt erst mit Restart).
2. **Ohne die Produktion umzukonfigurieren.** Denselben Balancer trifft
   jedes Schwestertool. Wenn eines davon einen Endpunkt hat, der sich
   verzögern lässt, misst man dort — ohne am Pseudonymisierer zu drehen.

### Regeln, die dabei gelten

* **Nur synthetische Dokumente.** Der Testweg geht durch die Produktion;
  echte Personendaten haben da nichts verloren (Hausregel in CLAUDE.md).
  `scripts/schwelle_messen.py` baut passende Dokumente bereits — seine
  Bauer (`baue_txt`, `baue_docx`, `baue_xlsx`) sind wiederverwendbar.
* **Die erhöhte Schwelle ist ein offenes Fenster.** Solange sie hoch steht,
  läuft jede grosse Datei synchron — auch die eines echten Nutzers. Kurz
  halten, und nicht über Nacht stehen lassen.
* **Der Deploy-Ausfall zählt mit.** Ein Restart kostet 3–6 Minuten in zwei
  Fenstern, mit einer trügerischen 200 dazwischen.

### Was danach zu tun ist

Den gemessenen Wert an die Stellen tragen, an denen heute „eine Minute"
oder „60 s" als Annahme markiert steht: `backend/config.py`
(`sync_threshold_chars`), `backend/routers/documents.py`
(`_laeuft_als_auftrag`), `scripts/schwelle_messen.py` (`BUDGET_SEKUNDEN`),
`.env.example`, CLAUDE.md und dieser Abschnitt. Fällt der Wert deutlich
anders aus, `SYNC_THRESHOLD_CHARS` mit
`scripts/schwelle_messen.py --budget <gemessen>` neu ableiten.
