# PDF und OCR (Phase 6 / Zyklus B) — Entwurf

**Datum:** 2026-08-28
**Baut auf:** Zyklus A (asynchrone Aufträge, 2026-08-25). Ein PDF-Lauf ist
speicher- und zeitintensiv und läuft über den bestehenden Auftragspfad.

---

## 1. Was Phase 6 ist — und was ausdrücklich nicht

Die Roadmap (Dossier §6, Adapter-Tabelle) staffelt es klar:

1. **PDF mit Textebene** — der Normalfall eines am Rechner erzeugten PDF: Der
   Text steht als solcher im Dokument, mit Positionsangaben. Erkennen und
   Ersetzen wie bei den anderen Formaten.
2. **OCR für Scans** — ein eingescanntes PDF trägt nur ein Bild je Seite,
   keinen Text. Texterkennung (OCR) macht daraus lesbaren Text mit
   Positionsangaben; danach wie oben.

**Ausdrücklich NICHT Teil davon:** das *formaterhaltende Neusetzen von
Glyphen* — ein PDF so zu verändern, dass der Ersatztext exakt in den
ursprünglichen Zeichenfluss eingepasst wird. Das ist laut Roadmap „ein
eigenes Vorhaben" und bleibt es.

Stattdessen — und das ist die tragende Designentscheidung — nutzt Phase 6
**Redaction**: Die gefundene Stelle wird aus dem PDF **entfernt** (nicht nur
optisch überdeckt) und das Pseudonym an ihre Stelle gestempelt. Das Layout
der Seite bleibt erhalten, nur die geschwärzte Stelle ändert sich. Für ein
Datenschutzwerkzeug ist das die *richtige* Semantik: Es geht nicht darum,
dass das Ergebnis hübsch aussieht, sondern dass der Klarname wirklich weg
ist.

---

## 2. Werkzeuge

**PyMuPDF (`pymupdf`, importiert als `fitz`).** Eine Bibliothek deckt alles
ab, was die Textebene braucht:

- `page.get_text("rawdict")` — Text je Seite, hinunter bis zur **einzelnen
  Glyphe mit ihrer Bounding-Box**. Das ist die Grundlage dafür, einen
  beliebigen Zeichenbereich eines Segments präzise zu verorten.
- `page.add_redact_annot(rect, …)` + `page.apply_redactions()` — entfernt den
  Inhalt unter dem Rechteck **aus dem Inhaltsstrom** (Text wie Bild), nicht
  nur sichtbar. Genau die Zusicherung, die ein Pseudonymisierer braucht.
- `page.get_pixmap()` — rastert eine Seite zu einem Bild, für OCR.
- `doc.set_metadata({})` / `doc.del_xml_metadata()` — Metadaten leeren.

Kein Systembinary für die Textebene. **PyMuPDF ist AGPL** — das ist bei einem
intern betriebenen Werkzeug ohne Weitergabe unbedenklich, gehört aber in
`NOTICE`/Doku vermerkt (der Auftraggeber entscheidet, falls die Lizenzlage je
anders wird).

**Tesseract (für OCR, Stufe 2).** `pytesseract` ruft das **Systembinary**
`tesseract` mit den Sprachdaten `deu`. Beides muss auf dem Knoten liegen —
und da der Benutzer `python` auf Jelastic kein `sudo` hat, ist das eine
**Deploy-Entscheidung**, kein `pip install`. Deshalb:

- OCR ist **hinter einem Schalter** (`OCR_ENABLED`, ab Werk aus) **und** einer
  Laufzeitprüfung auf die Verfügbarkeit des Binaries.
- Der Textebenen-Pfad läuft **ohne** Tesseract vollständig.

---

## 3. Der Adapter

`backend/adapters/pdf_adapter.py`, `class PdfAdapter(DocumentAdapter)`,
`extensions = (".pdf",)`, `content_types = ("application/pdf",)`. Registriert
in `adapters/__init__.py`; `.pdf` und `application/pdf` fallen aus
`_PLANNED`/`_PLANNED_MIME`.

Der Adapter erfüllt denselben Vertrag wie die übrigen (`adapters/base.py`):
zustandslos, deterministisch, überlappungsfreie Segmente, Roundtrip-Garantie
bei leerem `replacements`.

### 3.1 `extract_segments(data) -> List[Segment]`

- **Ein Segment = eine Textzeile** (`line` aus `rawdict`). Zeile ist die
  natürliche Einheit: genug Kontext für die NER, klein genug für eine
  eindeutige Stelle.
- `Segment.text` = die zusammengesetzten Zeichen der Zeile.
- `Segment.id` = sprechend und eindeutig, z. B. `"page3/line12"`; bei OCR
  `"page3/ocr/line5"`.
- `Segment.location` = eine **Adresse**, kein Objekt: `PdfLocation(page_index,
  line_index, quelle)` mit `quelle ∈ {"text", "ocr"}`. Die Boxen werden beim
  Zurückschreiben **neu aus denselben Bytes abgeleitet** (Determinismus-
  Pflicht), nicht in der Location mitgeschleppt — dasselbe Muster wie beim
  DOCX-Adapter, dessen Location `(Part-Name, Absatz-Index)` ist.
- Leere/whitespace-Zeilen dürfen entfallen.

### 3.2 `apply_replacements(data, replacements) -> bytes`

Für jede `(segment_id, [Replacement(start, end, text), …])`:

1. Location auflösen → Seite + Zeile, `rawdict` erneut lesen (deterministisch).
2. Für den Bereich `[start, end)` die **Vereinigung der Glyphen-Boxen** dieser
   Zeichen bilden → ein Redaction-Rechteck.
3. `page.add_redact_annot(rect)` sammeln; nach allen Annots der Seite einmal
   `page.apply_redactions()` — das **entfernt** den alten Inhalt.
4. Das Pseudonym an die Stelle setzen (`page.insert_textbox`/`insert_text` in
   das Rechteck, Schriftgrösse notfalls verkleinert, damit ein langes
   `[[ORGANIZATION_1]]` in eine kurze Namensbox passt). Passt es gar nicht,
   ist ein **Abschneiden akzeptabel** — der Klarname ist weg, das ist das
   Ziel; ein Hinweis gehört in den Modul-Docstring.

**Roundtrip:** leeres `replacements` → `doc` einmal neu speichern
(`doc.tobytes()`), inhaltsgleich; Byte-Gleichheit nicht zugesichert.

**Reihenfolge:** Redaction-Rechtecke je Seite gesammelt, dann *ein*
`apply_redactions()` — mehrfaches Anwenden je Rechteck verschöbe Koordinaten.

### 3.3 `strip_metadata(data) -> bytes`

`doc.set_metadata({})` (leert Autor, Titel, Ersteller …) **und**
`doc.del_xml_metadata()` (XMP). Rückgabe: neue Bytes.

---

## 4. OCR (Stufe 2) — sicher gestaffelt

**`backend/ocr/`** kapselt Tesseract, wie `backend/ki/` Infomaniak kapselt.

### 4.1 Wann OCR läuft

Beim `extract_segments`: Liefert eine Seite über die Textebene **keinen
nennenswerten Text** (Schwelle, z. B. < 10 Zeichen nach Trim), gilt sie als
**gescannt**. Dann:

- Ist OCR **verfügbar** (Schalter an *und* Binary da): Seite rastern, durch
  Tesseract, OCR-Segmente mit `quelle="ocr"` erzeugen.
- Ist OCR **nicht verfügbar**: siehe 4.3 — **lautes Scheitern**, niemals stumm.

### 4.2 Redaction auf einer Bildseite

Auch ein OCR-Fund wird geschwärzt: `add_redact_annot(rect)` +
`apply_redactions()` entfernt bei einer Bildseite die **Pixel** unter dem
Rechteck (PyMuPDF kann Bildredaction). Das Pseudonym wird darüber gestempelt.
Die Box kommt aus Tesseracts Wort-Boxen (Bild-Pixel → PDF-Punkte über den
Rasterungs-Zoom umgerechnet).

### 4.3 Die eine Regel, die alles trägt

**Ein Scan, dessen Text niemand lesen konnte, darf NIE als „verarbeitet"
durchgehen.** Eine gescannte Seite ohne Textebene, für die OCR ausfällt
(Schalter aus, Binary fehlt, Tesseract wirft), liefert **keine** Funde — und
ein Dokument ohne Funde sieht pseudonymisiert aus, obwohl der volle Klarname
als Bild darin steht. Das ist das gefährlichste Fehlerbild dieses ganzen
Werkzeugs.

Deshalb: Erkennt der Adapter eine **bildbasierte Seite** und kann sie **nicht**
per OCR lesen, wirft er `NutzermeldungError` (aus `backend/jobs/ausfuehrung.py`,
der durchreichbare Typ) mit einem klaren Text: „Dieses PDF enthält gescannte
Seiten; die Texterkennung ist auf dieser Installation nicht aktiviert. Bitte
als DOCX/TXT hochladen oder die Administration kontaktieren." Der Auftragspfad
macht daraus einen sauber gescheiterten Auftrag, der synchrone Pfad eine 415.
**Ein halb erkanntes Dokument ist gefährlicher als gar keines** — dieselbe
Logik wie bei der Fundliste, die erst bei fertigem Auftrag erscheint.

Der Wächter dafür ist ein Test: eine Bildseite ohne Textebene, OCR aus →
`NutzermeldungError`, **kein** stilles Ergebnis.

### 4.4 Speicher und Zeit

Gemessen (Design-Spec Zyklus A): ~2 s je Seite, ~1.1 GB Spitze je OCR-Lauf.
Der Auftrags-Arbeiter fährt bewusst **einen** Job je Worker (siehe
`arbeiter.py`) — genau dafür. Die Rasterungs-DPI ist der Hebel für den
Speicher; ein maßvoller Wert (z. B. 200 dpi) genügt Tesseract.

---

## 5. Datenschutz — was sich ändert und was nicht

**Die Textebene ändert nichts an der Datenschutzaussage:** Alles läuft lokal,
kein Byte verlässt den Server (die KI-Schicht bleibt orthogonal und optional).

**OCR läuft ebenfalls vollständig lokal** (Tesseract ist ein Systembinary auf
demselben Knoten). Es geht kein Bild und kein Text hinaus. Das ist der Grund,
Tesseract und nicht einen Cloud-OCR-Dienst zu wählen: Ein Cloud-OCR verschöbe
den gesamten Dokumentinhalt nach aussen und stünde im Widerspruch zur
Grundaussage des Werkzeugs.

Der Dossier-Passus (`landing`, §9) braucht **keine** Änderung durch Phase 6,
solange OCR lokal bleibt. (Zur Entscheidung des Auftraggebers, nicht hier
gesetzt.)

---

## 6. Wo Phase 6 den Baum berührt

| Datei | Änderung |
|---|---|
| `backend/adapters/pdf_adapter.py` | **neu** — der Adapter (Textebene + OCR-Haken) |
| `backend/adapters/__init__.py` | `PdfAdapter` registrieren, `.pdf` aus `_PLANNED` |
| `backend/ocr/` | **neu** — Tesseract-Kapsel + Verfügbarkeitsprüfung |
| `backend/config.py` | `ocr_enabled: bool = False`, Rasterungs-DPI, Schwelle |
| `pti-requirements.txt` | `pymupdf`, `pytesseract` (gepinnt) |
| `deploy/pythonenv` | `OCR_ENABLED` exportieren |
| `.env.example` | `OCR_ENABLED` dokumentieren |
| `backend/tests/test_adapter_pdf.py` | **neu** — Roundtrip, Redaction entfernt Text, Metadaten |
| `backend/tests/test_ocr*.py` | **neu** — OCR-Logik mit falscher Engine, der Wächter aus 4.3 |
| `golden-set/synthetic/generator.py` | ein PDF-Testdokument (Textebene) |
| `API.md` | PDF nicht mehr 415; die zwei Stufen erklärt |
| `CLAUDE.md` | Abschnitt PDF/OCR |
| Dossier (`landing`) | Roadmap-Punkt 6 auf „in Betrieb (Textebene)" — Auftraggeber |

---

## 7. Abnahme

Fachlich abgenommen, wenn durch Tests gedeckt:

1. **Textebenen-PDF, Roundtrip:** leeres `replacements` → inhaltsgleich.
2. **Redaction entfernt wirklich:** nach `apply_replacements` steht der
   Klarname **nicht mehr** im über `get_text` extrahierten Text des Ergebnis-
   PDF — geprüft, nicht nur überdeckt.
3. **Metadaten weg:** Autor/Titel nach `strip_metadata` leer.
4. **Ganze Pipeline:** ein PDF durch `anonymize_document`, ein Name über
   Zeilengrenzen wird ersetzt (Fund-Übertragung greift auch hier).
5. **Der Wächter aus 4.3:** Bildseite ohne Textebene, OCR aus →
   `NutzermeldungError`, kein stilles Ergebnis.
6. **OCR-Logik mit falscher Engine:** Wort-Boxen → Redaction-Rechtecke,
   ohne echtes Tesseract (wie die KI-Schicht mit einer Test-`ki`).

Dazu die Hausregeln: `ruff` grün, Golden-Set-Gate unverändert grün,
Frontend-Typprüfung und Build (das Frontend ändert sich für PDF nicht — es
lädt schon heute beliebige Dateien hoch; nur die Fehlermeldung entfällt).

---

## 8. Offen / bewusst zurückgestellt

- **Formaterhaltendes Glyphen-Neusetzen** — bleibt „ein eigenes Vorhaben".
- **OCR-Qualität** ist nicht ausgemessen; Tesseract `deu` auf Behörden-Scans
  kann Eigennamen verstümmeln. Ein erweitertes Golden-Set mit echten Scans
  scheidet aus (keine echten Personendaten); synthetische gescannte PDFs
  (gerenderter Text als Bild) messen nur den Idealfall.
- **Der Deploy von Tesseract** auf den Jelastic-Knoten ist ein eigener
  Schritt (Binary + `deu`-Sprachdaten), nicht Teil dieser Codeänderung.
