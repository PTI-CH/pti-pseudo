# Pseudonymisierer — Phase 1

**Datum:** 2026-08-18
**Status:** umgesetzt
**Grundlage:** Fachkonzept `landing/frontend/public/ideen/pseudonymizer.html`, §1–§15

## Ziel & Kontext

Der Pseudonymizer war bislang nur eine Kachel im Zustand `idea` auf dem TIP-Hub,
verlinkt auf ein 86 KB grosses Fachkonzept. Diese Etappe macht daraus ein
benutzbares Werkzeug: Phase 1 des Phasenplans (§12) — Engine und Oberfläche,
vollständig lokal, ohne asynchrone Aufträge, ohne Mandantenverwaltung und ohne
KI-Feinschliff.

Der Nutzen, der die Sache trägt: PTI-Mitarbeitende sollen Unterlagen an externe
Dienste geben können, ohne dass Personendaten das Haus verlassen — und die
Antwort danach wieder in den Originalzusammenhang zurückholen.

## Im Umfang

- Erkennung in Text und DOCX, dreistufig: Begriffsliste, Muster mit Prüfziffer,
  deutsche Namenserkennung
- Umkehrbare Pseudonymisierung mit verschlüsseltem Zuordnungstresor
- Endgültige Anonymisierung ohne Tresor
- Verlustfreie Rückführung
- Oberfläche auf Deutsch mit Verlässlichkeits-Ampel je Fund
- Anbindung an das TIP-Hub-Anmeldeverfahren und die Hub-Kachel

## Nicht im Umfang

Asynchrone Aufträge und Warteschlange (Phase 2), Mandantenfähigkeit mit
API-Schlüsseln (Phase 2), KI-Schicht für Koreferenz und Feinschliff (Phase 3),
PDF und Bilderkennung (Phase 5), XLSX und PPTX.

## Architektur

Ein einziges Python-Runtime. FastAPI liefert die REST-Schnittstelle **und** das
statisch gebaute React-SPA aus derselben Herkunft aus — dadurch entfällt CORS,
und das geteilte Sitzungs-Cookie des Hubs wird ohne Sonderregeln mitgeschickt.

```
Browser ──► NGINX ──► gunicorn (2 Worker, uvicorn)
                        │
                        ├── HttpsRedirect ──► HubAccessAuth ──► Router
                        │                          │
                        │                          └─► CRM /api/auth/session
                        │
                        └── backend/pipeline
                              ├── adapters   (DOCX, TXT, CSV)
                              ├── engine     (Presidio + spaCy + CH-Erkenner)
                              └── vault      (AES-GCM, PostgreSQL)
```

Presidio liegt als Kopie im Repository und wird per Editable-Install geladen,
nicht als PyPI-Paket bezogen. Damit sind Anpassungen an den Erkennern jederzeit
möglich — das war der ausdrückliche Grund für das Kopieren statt Einbinden.

## Drei Abweichungen vom Fachkonzept

Jede ist im Code an der betreffenden Stelle begründet.

### 1. Suchschlüssel als HMAC statt Klartext

§3 sah vor, neben dem verschlüsselten Original einen `cluster_key` im **Klartext**
zu speichern, damit gleiche Entitäten wiedergefunden werden. Damit stünde der
Klarname in der Nachbarspalte des Chiffrats — die Verschlüsselung wäre wertlos.

Stattdessen `cluster_key_hmac = HMAC-SHA256(mandantenschlüssel, normalisierter Text)`.
Der Gleichheits-Lookup funktioniert unverändert, der Klartext ist ohne Schlüssel
nicht rekonstruierbar.

### 2. Pseudonymform `[[PERSON_3]]` statt `Person B`

§7 schlug „Person B" vor. Das ist beim Rückführen nicht kollisionssicher: ein
Dokument, das die Zeichenfolge „Person B" legitim im Fliesstext enthält, würde
fälschlich zurückersetzt. Die doppelte Klammerform kommt in natürlicher Sprache
nicht vor. Das Format ist über `PSEUDONYM_TEMPLATE` konfigurierbar.

### 3. Mehrdeutige Typen werden nicht ungefragt ersetzt

Postleitzahl und Kontrollschild bekommen die Default-Policy `keep` statt
`redact`. `\b[1-9]\d{3}\b` trifft jede Jahreszahl, jeden Betrag und jedes
Aktenzeichen. Ungefragt ersetzt zerstören diese Erkenner mehr Text, als sie
schützen. Sie erscheinen im Befund und lassen sich dort einzeln bestätigen.

Zusätzlich zwingendes Kontext-Gating: Presidios eingebauter Mechanismus kann
Bewertungen nur **anheben**, nie unterdrücken. Beide Erkenner überschreiben
darum `analyze()` und verlangen ein Kontextwort im Umfeld.

## Gemessene Qualität

Am Integrationssatz, exakte Span-Übereinstimmung, sieben erwartete Entitäten:

| | Precision | Recall | F1 |
|---|---|---|---|
| mit Überlappungsauflösung | 1.000 | 1.000 | 1.000 |
| ohne | 0.583 | 1.000 | 0.737 |

Die Auflösung verwirft genau die fünf beobachteten Störtreffer: eine
E-Mail-Adresse, die gleichzeitig als Person und in Teilen zweimal als URL
erkannt wurde, sowie die generischen Telefon- und IBAN-Treffer, die gegen die
spezifischeren Schweizer Erkenner verlieren.

Entscheidungstest über die mehrdeutigen Erkenner: 14 von 14 korrekt. „im Jahr
2026", „CHF 8500", „Artikel 1234", „ZH 8000" und Nummern mit verfälschter
Prüfziffer feuern nicht; die zugehörigen Positivfälle feuern.

**Laufzeit:** Modell laden 1.15 s, Engine bauen 0.03 s, 411 MB RSS je Worker,
rund 23'400 Zeichen/s.

## Ein Befund, der den Betrieb betrifft

Das deutsche Modell hält **transliterierte Umlaute für Personennamen**:
„Spaeter" wird als PERSON erkannt, „Später" nicht. Das Wort in Ersatzschreibweise
stand nie in den Trainingsdaten und sieht damit aus wie ein unbekannter Eigenname.

Folgen: Eingaben sollten echte Umlaute enthalten, und Testdaten in diesem
Repository müssen es zwingend — sonst misst das Golden-Set die falsche Grösse.
Als Test festgehalten (`test_transliterierte_umlaute_erzeugen_fehlalarme`), damit
der Befund nicht wiederentdeckt werden muss.

## Sicherheit

- Dokument-Bytes werden nicht gespeichert, die Verarbeitung ist ephemer (§9).
- Tresor: AES-256-GCM, Schlüssel je Mandant über HKDF abgeleitet, Mandanten-ID
  als zusätzliche authentisierte Daten — ein Blob lässt sich nicht zwischen
  Mandanten verschieben.
- Nutzungserfassung schreibt ausschliesslich Metadaten und Zeichenzahlen, nie
  Inhalte und nie Dateinamen.
- Schreibrechte auf Begriffsliste und das Löschen von Tresoren hängen an der
  CRM-Rolle `admin`, bewusst nicht an einer Env-Liste, die auseinanderlaufen kann.
- `/api/health` ist die einzige Route ohne Anmeldung.

## Grenzen, die bekannt sind

Aus dem DOCX-Adapter, jeweils im Modul-Docstring festgehalten:

- `w:delText` — gelöschter Text aus nachverfolgten Änderungen. Unsichtbar, aber
  in der Datei. Änderungen vor der Verarbeitung annehmen.
- `w:instrText` — Feldbefehle wie `HYPERLINK "mailto:…"`.
- `w:author` an Kommentaren und Änderungsmarken: echte Personendaten, die
  `strip_metadata` bewusst nicht anfasst.
- `docProps/thumbnail.jpeg` zeigt die erste Seite als Bild.
- Eingebettete OLE-Objekte und Text in Bildern.
- CSV wird an physischen Zeilenumbrüchen getrennt, nicht CSV-korrekt geparst.
  Ein echter Parser würde beim Zusammensetzen das Quoting normalisieren und
  damit die Formaterhaltung verletzen.

## Verifikation

408 Tests, davon 241 in der Engine, 53 in den Adaptern, 50 im Zugriffsschutz und
16 als Ende-zu-Ende-Lauf über die echte HTTP-Schnittstelle.

Der DOCX-Adapter wurde zusätzlich mit **21 gezielten Mutationen** geprüft — 20
davon lassen die Tests rot werden, darunter die Umkehr der Ersetzungsreihenfolge
und das Abschalten des lxml-Durchlaufs. Das ist der Nachweis, dass die grünen
Tests tatsächlich etwas prüfen.
