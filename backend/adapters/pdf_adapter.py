"""Adapter für PDF-Dokumente (``.pdf``) — nur Textebene.

Anders als bei den OOXML-Formaten gibt es in einem PDF keine Absätze, Runs
oder Zellen, denen ein Ersatztext formaterhaltend eingefügt werden könnte.
Ein PDF trägt Text als Folge platzierter Glyphen mit fester Position; ein
Zeichenbereich formaterhaltend gegen einen anders langen Ersatztext
auszutauschen hiesse, den kompletten nachfolgenden Zeichenfluss der Seite neu
zu setzen. Laut Roadmap ist genau das **„ein eigenes Vorhaben"** und nicht
Teil dieses Adapters (siehe ``docs/superpowers/specs/2026-08-28-pdf-ocr-design.md``,
§1).

Stattdessen benutzt dieser Adapter **Redaction**: Die gefundene Stelle wird
mit ``page.add_redact_annot()`` + ``page.apply_redactions()`` aus dem
Inhaltsstrom **entfernt** — nicht nur optisch überdeckt —, und das Pseudonym
wird anschliessend in das freigewordene Rechteck gestempelt. Für ein
Datenschutzwerkzeug ist das die richtige Semantik: Es geht nicht darum, dass
das Ergebnis wie das Original aussieht, sondern dass der Klarname wirklich
weg ist (Spec §1).

Ein Segment ist eine Textzeile
-------------------------------
``page.get_text("rawdict")`` liefert Text bis hinunter zur einzelnen Glyphe
mit ihrer Bounding-Box: ``blocks`` → ``lines`` → ``spans`` → ``chars``, jedes
Zeichen mit ``"c"`` (das Zeichen selbst) und ``"bbox"`` (seine Box). Eine
**Zeile** ist die natürliche Segment-Einheit: genug Kontext für die
NER-Erkennung, klein genug für eine eindeutige Stelle (Spec §3.1).

``Segment.text`` entsteht durch Aneinanderreihen der ``"c"``-Zeichen aller
``spans`` einer ``line``, in genau der Reihenfolge, in der ``rawdict`` sie
liefert (siehe ``_line_chars``/``_line_text``). Diese Reihenfolge — nicht
etwa eine Sortierung nach Position — legt fest, welches Offsetpaar in
:class:`~backend.adapters.base.Replacement` auf welches Zeichen zeigt. Beim
Zurückschreiben wird ``rawdict`` **erneut** aus denselben Bytes gelesen
(Determinismus-Pflicht aus ``adapters/base.py``): ``PdfLocation`` trägt keine
Boxen, nur ``(page_index, line_index, quelle)`` — dieselbe Adresse-statt-
Objekt-Regel wie beim DOCX-Adapter.

Schriftverkleinerung, dann Abschneiden
---------------------------------------
Ein Ersatzplatzhalter wie ``[[ORGANIZATION_1]]`` ist fast immer länger als
der Klarname, den er ersetzt — die Redaction-Box ist aber genau so gross wie
der Klarname war. ``_insert_pseudonym`` probiert deshalb absteigende
Schriftgrössen (``_FONT_SIZES``); passt der Platzhalter bei keiner davon,
wird er bei der kleinsten Grösse von hinten so weit gekürzt, bis er passt.
Der Klarname ist in jedem Fall bereits entfernt, bevor das entschieden wird
— das Abschneiden betrifft ausschliesslich die Lesbarkeit des Platzhalters,
nie den Datenschutz (Spec §3.2). ``page.insert_textbox`` selbst zeichnet bei
zu wenig Platz nachweislich **nichts** (siehe Testnachweis im Modulkopf der
Testdatei) statt sichtbar zu überlaufen — genau deshalb der eigene
Kürzungs-Loop statt eines einzelnen Versuchs.

Nicht abgedeckt (bewusst)
--------------------------
* **Formularfelder (AcroForm) und Anmerkungen** ausserhalb der Textebene —
  ein Kommentar oder ein ausgefülltes Formularfeld kann Personendaten tragen,
  wird von ``get_text("rawdict")`` aber nicht erfasst. Eigenes Vorhaben.
* **Eingebettete Dateien** (``/EmbeddedFiles``) und **Lesezeichen**
  (``/Outlines``) — dieselbe Begründung.
* **Formaterhaltendes Glyphen-Neusetzen** — siehe oben, laut Roadmap
  ausdrücklich ausgeklammert.

OCR (Spec §4) — Stufe 2 für gescannte Seiten
----------------------------------------------
Liefert eine Seite über die Textebene **keinen nennenswerten Text**
(``_MIN_TEXT_CHARS_JE_SEITE`` = 10 getrimmte Zeichen über die ganze Seite),
prüft ``_segmente_fuer_bildseite`` zunächst, ob die Seite überhaupt ein Bild
trägt (``page.get_images()``). Drei Fälle:

1. **Kein Bild** — eine echte leere Trennseite: kein Text, kein Bild. Es gibt
   nichts zu erkennen; die Methode liefert ``[]``, kein Fehler.
2. **Bild vorhanden, OCR verfügbar** (:func:`backend.ocr.ocr_verfuegbar`):
   Die Seite wird gerastert (``page.get_pixmap(dpi=…)``), durch die
   :class:`~backend.ocr.OcrEngine` geschickt und zu Zeilen gruppiert
   (:func:`backend.ocr.gruppiere_zeilen`) — eine Tesseract-„Zeile" ist ein
   Segment, mit ``PdfLocation(page_index, line_index, "ocr")`` und
   ``id = f"page{p}/ocr/line{n}"``.
3. **Bild vorhanden, OCR NICHT verfügbar** (Schalter aus oder Binary fehlt):
   ``NutzermeldungError`` — **die tragende Sicherheitsregel des ganzen
   Vorhabens** (Spec §4.3): Ein Scan, dessen Text niemand lesen konnte, darf
   nie als „verarbeitet" durchgehen. Ein Dokument ohne Funde sieht
   pseudonymisiert aus, auch wenn der Klarname als Bild darin steht — ein
   lauter Fehlschlag ist deshalb sicherer als ein still durchgereichter,
   scheinbar sauberer leerer Fund. Im Zweifel (z. B. ein Fehler beim Lesen
   der Bildliste) nimmt ``_seite_traegt_bild`` ein Bild **an** — derselbe
   Grundsatz: ein unnötiger lauter Fehlschlag ist ungefährlich, eine
   übersehene Bildseite nicht.

**Redaction auf einer OCR-Seite** (in ``apply_replacements``) unterscheidet
sich von der Textebene in der Granularität: Tesseract liefert Wort-, keine
Zeichen-Boxen. Jedes Wort, dessen Zeichenbereich ``[start, end)``
überschneidet, wird deshalb vollständig geschwärzt — lieber etwas zu viel
überdeckt als ein Klarname, der zur Hälfte stehen bleibt.

**OCR läuft dadurch zweimal** — einmal beim Extrahieren, einmal beim
Anwenden, weil ``PdfLocation`` (Determinismus-Pflicht) keine Boxen
mitführt, sondern sie aus denselben Bytes neu ableitet. Für v1 hingenommen:
Ein PDF mit Scan-Seiten läuft ohnehin über den Auftragspfad (Spec §4.4, ein
Job je Worker), und ein zweiter, identischer Tesseract-Lauf ist dort ein
Kostenfaktor, kein Korrektheitsrisiko. Offene Optimierung: die Zeilen des
ersten Laufs für den zweiten zwischenspeichern (z. B. im Auftrag selbst) —
heute wird bewusst nichts zwischen den beiden Aufrufen geteilt, damit der
Adapter weiterhin zustandslos bleibt (``adapters/base.py``).

Engine und Verfügbarkeitsprüfung sind über den Konstruktor injizierbar
(``PdfAdapter(ocr_engine=…, ocr_verfuegbar=…, ocr_dpi=…)``) — dasselbe
Muster wie der HTTP-Client in ``backend/ki/infomaniak.py``. Ohne Injektion
verwendet der Adapter :class:`backend.ocr.TesseractEngine` und
:func:`backend.ocr.ocr_verfuegbar` mit den echten Einstellungen. Die
Registry (``adapters/__init__.py``) instanziiert ``PdfAdapter()`` ohne
Argumente; die Injektion ist ausschliesslich für Tests gedacht.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import pymupdf

from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.ocr import (
    OcrEngine,
    OcrWort,
    OcrZeile,
    TesseractEngine,
    gruppiere_zeilen,
    pixel_zu_punkt,
)
from backend.ocr import ocr_verfuegbar as _ocr_verfuegbar_standard

__all__ = ["PdfAdapter", "PdfLocation"]


#: Seiten mit weniger als dieser Zahl getrimmter Zeichen über die ganze Seite
#: gelten als bildbasiert (gescannt) — Schwelle aus Spec §4.1.
_MIN_TEXT_CHARS_JE_SEITE = 10

#: Basis-14-Schriftart für eingesetzte Pseudonyme. Jeder PDF-Betrachter kennt
#: sie ohne Einbettung; Umlaute in Platzhaltern kommen ohnehin nicht vor
#: (Platzhalter sind ASCII, siehe ``adapters/base.py``).
_FONT = "helv"

#: Schriftgrössen, absteigend probiert, bevor der Platzhaltertext selbst
#: gekürzt wird (siehe Modul-Docstring, Abschnitt „Schriftverkleinerung").
_FONT_SIZES: Tuple[float, ...] = (11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0)

#: Feste Ersatzgrösse für die Einfüge-Box einer reinen Einfügung
#: (``repl.start == repl.end``) auf einer sonst völlig leeren Zeile — ein
#: Randfall, den die Erkennung in der Praxis nicht erzeugt (Funde sind stets
#: nicht-leere Textspannen), aber strukturell möglich laut Vertrag.
_LEERE_ZEILE_HOEHE = 14.0

#: Breitenreserve nach rechts für die Einfüge-Box einer reinen Einfügung.
_EINFUEGE_BREITE = 200.0


@dataclass(frozen=True)
class PdfLocation:
    """Adresse einer Textzeile im PDF — für Aufrufer opak.

    Bewusst nur primitive Werte, keine Glyphen-Boxen: Zwischen
    ``extract_segments()`` und ``apply_replacements()`` liegt möglicherweise
    ein anderer HTTP-Request, und ``rawdict`` wird dann erneut aus denselben
    Bytes gelesen (Determinismus-Pflicht, siehe Modul-Docstring).

    Attributes
    ----------
    page_index
        0-basierter Seitenindex.
    line_index
        0-basierter, fortlaufender Index der Zeile innerhalb der Textblöcke
        der Seite, in der Reihenfolge, in der ``rawdict`` Blöcke und Zeilen
        liefert (siehe ``_iter_page_lines``). Stabil, weil dieser Adapter
        beim Zurückschreiben niemals Zeilen, Blöcke oder Zeichen einfügt
        oder entfernt, bevor die Zeilen-Landkarte für eine Seite gebaut ist.
    quelle
        ``"text"`` für eine Zeile aus der Textebene, ``"ocr"`` für eine
        Tesseract-„Zeile" einer gescannten Seite (Spec §4). Bestimmt in
        ``apply_replacements``, ob die Boxen aus ``rawdict``-Zeichen oder
        aus einem erneuten OCR-Lauf abgeleitet werden — beide Boxen sind
        unterschiedlich granular (Zeichen vs. Wort) und liegen in
        unterschiedlichen Koordinatenräumen (PDF-Punkte direkt vs.
        Bild-Pixel, umgerechnet über den Rasterungs-Zoom).
    """

    page_index: int
    line_index: int
    quelle: str


def _line_chars(line: dict) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """Zerlegt eine ``rawdict``-Zeile in (Zeichen, Glyphen-Box)-Paare.

    Die Zeichen werden Span für Span, innerhalb eines Spans Zeichen für
    Zeichen aneinandergereiht — exakt die Reihenfolge, in der PyMuPDF sie in
    ``line["spans"][i]["chars"]`` liefert. Diese Reihenfolge legt fest, was
    ``Segment.text`` ist und wie seine Offsets zu verstehen sind;
    ``_build_segments`` und ``apply_replacements`` gehen deshalb beide über
    genau diese Funktion und nie über einen eigenen Weg.
    """
    chars: List[Tuple[str, Tuple[float, float, float, float]]] = []
    for span in line.get("spans", ()):
        for ch in span.get("chars", ()):
            chars.append((ch["c"], ch["bbox"]))
    return chars


def _line_text(chars: List[Tuple[str, Tuple[float, float, float, float]]]) -> str:
    """Setzt den Zeilentext aus (Zeichen, Box)-Paaren zusammen."""
    return "".join(c for c, _ in chars)


def _iter_page_lines(page_dict: dict):
    """Liefert ``(line_index, chars)`` für jede Zeile jedes Textblocks.

    Bildblöcke (``block["type"] != 0``) werden übersprungen. Der Index zählt
    über **alle** Textblöcke einer Seite fortlaufend hoch — er hängt nur von
    der ``rawdict``-Struktur ab, nie davon, ob eine Zeile leer ist oder
    später als Segment auftaucht, und ist damit über zwei getrennte Aufrufe
    auf identische Bytes stabil (Determinismus-Pflicht).
    """
    line_index = 0
    for block in page_dict.get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            yield line_index, _line_chars(line)
            line_index += 1


def _union_rect(
    chars: List[Tuple[str, Tuple[float, float, float, float]]],
    start: int,
    end: int,
) -> pymupdf.Rect:
    """Vereinigt die Glyphen-Boxen der Zeichen ``[start, end)`` zu einem Rechteck."""
    rect = pymupdf.Rect(chars[start][1])
    for index in range(start + 1, end):
        rect |= pymupdf.Rect(chars[index][1])
    return rect


def _insertion_rect(
    chars: List[Tuple[str, Tuple[float, float, float, float]]], index: int
) -> pymupdf.Rect:
    """Baut eine Box für eine reine Einfügung (``repl.start == repl.end``).

    Ohne gelöschten Text gibt es keine Glyphen-Vereinigung, die eine Box
    definieren könnte. Verwendet wird deshalb der linke Rand des Zeichens an
    ``index`` als Einfügepunkt — oder, steht die Einfügung nach dem letzten
    Zeichen der Zeile, dessen rechter Rand —, mit dessen Zeilenhöhe und einer
    grosszügigen Breitenreserve nach rechts (``_EINFUEGE_BREITE``): lieber
    überschreibt das Pseudonym ungenutzten Raum rechts daneben, als spurlos
    zu verschwinden. Auf einer völlig leeren Zeile (kein Nachbarzeichen)
    kommt eine feste Standardgrösse zum Einsatz.
    """
    if not chars:
        return pymupdf.Rect(
            36.0, 36.0, 36.0 + _EINFUEGE_BREITE, 36.0 + _LEERE_ZEILE_HOEHE
        )
    if index < len(chars):
        _, bbox = chars[index]
        x0, y0, _x1, y1 = bbox
    else:
        _, bbox = chars[-1]
        _x0, y0, x0, y1 = bbox
    return pymupdf.Rect(x0, y0, x0 + _EINFUEGE_BREITE, y1)


def _ocr_wort_rect(wort: OcrWort, dpi: int) -> pymupdf.Rect:
    """Wandelt die Pixel-Box eines :class:`~backend.ocr.OcrWort` in Punkte um.

    Die zwei Ecken der Box werden einzeln über
    :func:`backend.ocr.pixel_zu_punkt` umgerechnet (Spec §4.2: „Bild-Pixel →
    PDF-Punkte über den Rasterungs-Zoom").
    """
    x0, y0 = pixel_zu_punkt(wort.left, wort.top, dpi)
    x1, y1 = pixel_zu_punkt(wort.left + wort.width, wort.top + wort.height, dpi)
    return pymupdf.Rect(x0, y0, x1, y1)


def _ocr_treffer(zeile: OcrZeile, start: int, end: int) -> List[OcrWort]:
    """Woerter einer OCR-Zeile, deren Zeichenbereich ``[start, end)`` schneidet.

    Wort-granular statt zeichen-genau (Spec §3.2/§4.2): Tesseract liefert nur
    Wort-Boxen, kein einzelnes Zeichen ist adressierbar. Ein Fund, der nur
    einen Teil eines Wortes trifft — zum Beispiel weil die Erkennung mitten im
    Wort endete —, schwärzt deshalb das **ganze** Wort: lieber etwas zu viel
    überdeckt, als dass ein Namensrest sichtbar bleibt.
    """
    return [wort for (s, e, wort) in zeile.wort_spannen if s < end and e > start]


def _ocr_union_rect(
    zeile: OcrZeile, start: int, end: int, dpi: int
) -> Optional[pymupdf.Rect]:
    """Vereinigt die Boxen aller Treffer-Woerter zu einem Rechteck.

    Liefert ``None``, wenn kein Wort den Bereich schneidet — bei
    identischen Bytes/Engine/Sprache kann das nur bei einer reinen
    Einfügung passieren, die exakt zwischen zwei Woerter fällt (siehe
    ``_ocr_insertion_rect``).
    """
    treffer = _ocr_treffer(zeile, start, end)
    if not treffer:
        return None
    rect = _ocr_wort_rect(treffer[0], dpi)
    for wort in treffer[1:]:
        rect |= _ocr_wort_rect(wort, dpi)
    return rect


def _ocr_insertion_rect(zeile: OcrZeile, index: int, dpi: int) -> pymupdf.Rect:
    """Box für eine reine Einfügung auf einer OCR-Zeile — Randfall.

    Analog zu ``_insertion_rect`` für die Textebene: Es gibt nichts zu
    schwärzen, nur eine Stelle, an die der Platzhalter gesetzt wird. Ohne
    Zeichen-Boxen ist das erste Wort, dessen Bereich ``index`` erreicht oder
    überschreitet, der beste verfügbare Anker; liegt ``index`` hinter dem
    letzten Wort, wird an dessen rechten Rand angesetzt. Eine Zeile ohne
    Wörter kann laut :func:`backend.ocr.gruppiere_zeilen` nicht vorkommen,
    der leere Fall bleibt trotzdem als Sicherheitsnetz erhalten.
    """
    if not zeile.wort_spannen:
        return pymupdf.Rect(
            36.0, 36.0, 36.0 + _EINFUEGE_BREITE, 36.0 + _LEERE_ZEILE_HOEHE
        )
    for _start, ende, wort in zeile.wort_spannen:
        if index <= ende:
            rect = _ocr_wort_rect(wort, dpi)
            return pymupdf.Rect(rect.x0, rect.y0, rect.x0 + _EINFUEGE_BREITE, rect.y1)
    _start, _ende, letztes_wort = zeile.wort_spannen[-1]
    rect = _ocr_wort_rect(letztes_wort, dpi)
    return pymupdf.Rect(rect.x1, rect.y0, rect.x1 + _EINFUEGE_BREITE, rect.y1)


def _validate_replacements(
    replacements: List[Replacement], text_length: int
) -> List[Replacement]:
    """Prüft Offsetgrenzen und Überlappung; liefert aufsteigend sortiert zurück.

    Eigenständig statt aus ``backend.adapters.ooxml`` übernommen: Jenes Modul
    ist ausdrücklich die „gemeinsame Grundlage der Office-Open-XML-Adapter"
    (docx/xlsx/pptx, siehe dessen Modul-Docstring) — ein PDF-Import von dort
    wäre eine unnötige Kopplung an ein namentlich XML-spezifisches Modul für
    eine Prüfung ohne jede XML-Abhängigkeit. Anders als beim DOCX-Adapter ist
    hier auch keine Reihenfolge fürs Anwenden nötig (siehe
    ``PdfAdapter.apply_replacements``): Jedes Rechteck wird unabhängig aus
    Glyphen-Boxen gebildet, nicht durch Textsplicing, das nachfolgende
    Offsets verschieben würde.
    """
    ordered = sorted(replacements, key=lambda r: (r.start, r.end))
    previous_end = 0
    for repl in ordered:
        if repl.end > text_length:
            raise ValueError(
                f"Ersetzung {repl.start}-{repl.end} liegt ausserhalb des "
                f"Segments (Länge {text_length})"
            )
        if repl.start < previous_end:
            raise ValueError(
                f"Überlappende Ersetzungen: {repl.start}-{repl.end} "
                f"überschneidet sich mit der vorangehenden, die bis "
                f"{previous_end} reicht"
            )
        previous_end = repl.end
    return ordered


def _insert_pseudonym(page: pymupdf.Page, rect: pymupdf.Rect, text: str) -> None:
    """Setzt ``text`` in ``rect`` — Schriftverkleinerung zuerst, dann Kürzung.

    ``page.insert_textbox`` liefert bei zu wenig Platz eine **negative**
    Restfläche und zeichnet dabei nachweislich **nichts** (siehe
    Selbstnachweis in der Testdatei) — kein halb sichtbarer Text, kein
    Überlauf. Deshalb zunächst der Reihe nach ``_FONT_SIZES`` probieren; passt
    der Platzhalter bei keiner Grösse, wird er bei der kleinsten Grösse
    zeichenweise von hinten gekürzt, bis er passt oder nur noch ein Zeichen
    übrig ist. Der Klarname ist zu diesem Zeitpunkt bereits entfernt — das
    Abschneiden betrifft ausschliesslich die Lesbarkeit des Platzhalters
    (Spec §3.2: „Abschneiden ist akzeptabel").
    """
    for fontsize in _FONT_SIZES:
        if page.insert_textbox(rect, text, fontsize=fontsize, fontname=_FONT) >= 0:
            return
    fontsize = _FONT_SIZES[-1]
    truncated = text
    while len(truncated) > 1:
        truncated = truncated[:-1]
        if (
            page.insert_textbox(rect, truncated, fontsize=fontsize, fontname=_FONT)
            >= 0
        ):
            return
    # Letzter Ausweg: erzwingen, auch wenn die Restfläche negativ bleibt —
    # besser ein sichtbares einzelnes Zeichen als spurlos nichts.
    page.insert_textbox(rect, text[:1], fontsize=fontsize, fontname=_FONT)


def _open(data: bytes) -> pymupdf.Document:
    """Öffnet PDF-Bytes; übersetzt PyMuPDFs Fehlerklasse in ``ValueError``.

    ``pymupdf.open`` wirft bei kaputten oder fremden Bytes ein
    ``pymupdf.FileDataError`` — ein ``RuntimeError``. Der Vertrag aus
    ``adapters/base.py`` verlangt für nicht zum Format passende Daten
    ``ValueError``.
    """
    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except pymupdf.FileDataError as exc:
        raise ValueError(f"Ungültiges oder beschädigtes PDF: {exc}") from exc


class PdfAdapter(DocumentAdapter):
    """Adapter für PDF-Dokumente — Textebene, Redaction statt Neusetzen.

    Die OCR-Abhängigkeiten (Engine, Verfügbarkeitsprüfung, Rasterungs-DPI)
    sind über den Konstruktor **injizierbar** — dasselbe Muster wie der
    HTTP-Client in ``backend/ki/infomaniak.py``. Ohne Injektion (der Weg, auf
    dem die Registry in ``adapters/__init__.py`` ``PdfAdapter()`` ohne
    Argumente instanziiert) verwendet der Adapter
    :class:`backend.ocr.TesseractEngine` und :func:`backend.ocr.ocr_verfuegbar`
    mit den echten, prozessweiten Einstellungen. Tests injizieren stattdessen
    eine Fake-Engine und/oder eine feste Verfügbarkeit, ganz ohne echtes
    Tesseract-Binary (Spec §4, Abnahme Punkt 6).
    """

    content_types = ("application/pdf",)
    extensions = (".pdf",)

    def __init__(
        self,
        *,
        ocr_engine: Optional[OcrEngine] = None,
        ocr_verfuegbar: Optional[Callable[[], bool]] = None,
        ocr_dpi: Optional[int] = None,
    ) -> None:
        """Nimmt optionale Test-Doubles für die OCR-Stufe entgegen.

        Parameters
        ----------
        ocr_engine
            Ersetzt :class:`backend.ocr.TesseractEngine`. ``None`` erzeugt
            sie bei Bedarf lazy — nie im Konstruktor, damit ein Adapter ohne
            jede gescannte Seite im Dokument nie ``pytesseract`` anfasst.
        ocr_verfuegbar
            Ersetzt :func:`backend.ocr.ocr_verfuegbar`. Ohne Argumente
            aufgerufen; wer die Einstellungen mocken will, kapselt das in
            diesem Callable (siehe Testdatei).
        ocr_dpi
            Ersetzt ``settings.ocr_dpi``. ``None`` liest die echten
            Einstellungen bei jedem Bedarf neu — genau wie
            ``ki_zweitmeinung`` woanders im Baum ohne Dashboard-Restart
            wirken soll.
        """
        self._ocr_engine = ocr_engine
        self._ocr_verfuegbar_pruefen = ocr_verfuegbar
        self._ocr_dpi = ocr_dpi

    def _hole_ocr_engine(self) -> OcrEngine:
        """Liefert die injizierte Engine oder erzeugt die echte, lazy."""
        if self._ocr_engine is not None:
            return self._ocr_engine
        return TesseractEngine()

    def _pruefe_ocr_verfuegbar(self) -> bool:
        """Liefert, ob OCR jetzt laufen darf — injiziert oder echt geprüft."""
        if self._ocr_verfuegbar_pruefen is not None:
            return self._ocr_verfuegbar_pruefen()
        return _ocr_verfuegbar_standard()

    def _resolve_ocr_dpi(self) -> int:
        """Liefert die Rasterungs-DPI — injiziert oder aus den Einstellungen."""
        if self._ocr_dpi is not None:
            return self._ocr_dpi
        from backend.config import get_settings

        return get_settings().ocr_dpi

    def _ocr_zeilen(self, page: pymupdf.Page, dpi: int) -> List[OcrZeile]:
        """Rastert ``page`` und liefert ihre OCR-Zeilen (Spec §4.1/§4.2).

        Wird sowohl beim Extrahieren als auch beim Anwenden aufgerufen — mit
        identischem ``dpi`` liefert das bei identischen Bytes und derselben
        Engine dieselbe Zeilenliste (Determinismus-Pflicht, siehe
        Modul-Docstring „OCR läuft dadurch zweimal").
        """
        pixmap = page.get_pixmap(dpi=dpi)
        bild = pixmap.pil_image()
        woerter = self._hole_ocr_engine().erkenne_seite(bild, sprache="deu")
        return gruppiere_zeilen(woerter)

    @staticmethod
    def _seite_traegt_bild(page: pymupdf.Page) -> bool:
        """Ob die Seite (mindestens) ein Bild referenziert — Scan-Verdacht.

        ``page.get_images(full=True)`` listet die Bild-XObjects, die der
        Content-Stream der Seite referenziert. Schlägt das Lesen fehl, wird
        ein Bild **angenommen** — im Zweifel ein unnötiger lauter
        ``NutzermeldungError`` statt einer übersehenen Scan-Seite (Spec §4,
        „im Zweifel Bild vorhanden annehmen").
        """
        try:
            return bool(page.get_images(full=True))
        except Exception:  # noqa: BLE001 — jeder Lesefehler zählt als "Bild da"
            return True

    # -- Extraktion ----------------------------------------------------

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert ein Segment je Textzeile, seitenweise in Lesereihenfolge."""
        doc = _open(data)
        try:
            return self._build_segments(doc)
        finally:
            doc.close()

    def _build_segments(self, doc: pymupdf.Document) -> List[Segment]:
        """Baut die vollständige Segmentliste eines bereits geöffneten Dokuments.

        Wird sowohl von ``extract_segments`` als auch von
        ``apply_replacements`` benutzt, damit beide exakt dieselben IDs und
        Locations sehen (Determinismus-Pflicht, siehe ``adapters/base.py``).
        """
        segments: List[Segment] = []
        for page_index in range(doc.page_count):
            page = doc[page_index]
            if len(page.get_text("text").strip()) < _MIN_TEXT_CHARS_JE_SEITE:
                segments.extend(
                    self._segmente_fuer_bildseite(doc, page, page_index)
                )
                continue
            page_dict = page.get_text("rawdict")
            for line_index, chars in _iter_page_lines(page_dict):
                text = _line_text(chars)
                if not text.strip():
                    continue  # leere/weisse Zeilen tragen keine PII
                segments.append(
                    Segment(
                        id=f"page{page_index}/line{line_index}",
                        text=text,
                        location=PdfLocation(page_index, line_index, "text"),
                    )
                )
        return segments

    def _segmente_fuer_bildseite(
        self, doc: pymupdf.Document, page: pymupdf.Page, page_index: int
    ) -> List[Segment]:
        """OCR-Nahtstelle für bildbasierte (gescannte) Seiten — Spec §4.

        Eine Seite gilt als bildbasiert, wenn ihre Textebene weniger als
        ``_MIN_TEXT_CHARS_JE_SEITE`` getrimmte Zeichen über die **ganze**
        Seite liefert (Spec §4.1) — das trifft auch eine wirklich leere Seite
        ohne jedes Bild. Diese Methode unterscheidet deshalb selbst noch
        einmal, **ob** überhaupt ein Bild da ist (``_seite_traegt_bild``),
        bevor sie OCR überhaupt in Erwägung zieht:

        1. **Kein Bild** — eine echte leere Trennseite. Es gibt nichts zu
           erkennen: ``[]``, kein Fehler.
        2. **Bild vorhanden, OCR verfügbar** — Seite rastern, durch die
           Engine, zu Zeilen gruppieren, je Zeile ein Segment mit
           ``PdfLocation(page_index, line_index, "ocr")``.
        3. **Bild vorhanden, OCR NICHT verfügbar** — :class:`NutzermeldungError`,
           den einzigen durchreichbaren Fehlertyp des Auftragspfads
           (``backend/jobs/ausfuehrung.py``), damit ein Scan ohne lesbaren
           Text **laut** scheitert statt als leeres, scheinbar sauberes
           Ergebnis durchzugehen (Spec §4.3 — die tragende Sicherheitsregel
           dieses ganzen Vorhabens: „Ein halb erkanntes Dokument ist
           gefährlicher als gar keines").

        Parameters
        ----------
        doc
            Das geöffnete Dokument. Ungenutzt in dieser Methode (die
            Rasterung läuft über ``page`` allein) — Teil der Signatur, damit
            der Aufruf in ``_build_segments`` unverändert bleibt.
        page
            Die als textarm erkannte Seite.
        page_index
            Ihr 0-basierter Index, für ``PdfLocation`` und die Segment-ID.

        Returns
        -------
        list of Segment
            Leer für eine echte leere Trennseite; sonst ein Segment je
            OCR-Zeile.

        Raises
        ------
        NutzermeldungError
            Wenn die Seite ein Bild trägt, OCR aber nicht verfügbar ist.
        """
        if not self._seite_traegt_bild(page):
            return []  # echte leere Trennseite: kein Text, kein Bild

        if not self._pruefe_ocr_verfuegbar():
            # Lokaler statt Modul-weiter Import: ``backend.jobs.ausfuehrung``
            # importiert ``backend.pipeline``, das seinerseits über
            # ``backend.adapters`` (diesen Adapter eingeschlossen) importiert
            # wird. Ein Import auf Modulebene wäre beim allerersten Laden von
            # ``backend.pipeline`` ein ``ImportError`` auf einen zu diesem
            # Zeitpunkt erst halb aufgebauten Namensraum — derselbe
            # Zirkel-Trick wie in ``backend/pipeline.py`` für die
            # ``backend.ki``-Module.
            from backend.jobs.ausfuehrung import NutzermeldungError

            raise NutzermeldungError(
                "Dieses PDF enthält gescannte Seiten; die Texterkennung ist "
                "auf dieser Installation nicht aktiviert. Bitte als DOCX/TXT "
                "hochladen oder die Administration kontaktieren."
            )

        zeilen = self._ocr_zeilen(page, self._resolve_ocr_dpi())
        return [
            Segment(
                id=f"page{page_index}/ocr/line{line_index}",
                text=zeile.text,
                location=PdfLocation(page_index, line_index, "ocr"),
            )
            for line_index, zeile in enumerate(zeilen)
        ]

    # -- Ersetzung -------------------------------------------------------

    def apply_replacements(
        self,
        data: bytes,
        replacements: Dict[str, List[Replacement]],
    ) -> bytes:
        """Schwärzt jede Ersetzung und stempelt das Pseudonym darüber (Spec §3.2).

        Ablauf je Seite: Erst **alle** Redaction-Rechtecke der Seite sammeln,
        dann **einmal** ``page.apply_redactions()`` — mehrfaches Anwenden je
        Rechteck würde nachfolgende Koordinaten verschieben (Spec §3.2,
        „Reihenfolge"). Erst danach werden die Pseudonyme eingesetzt, denn
        ``apply_redactions()`` würde einen zuvor eingesetzten Platzhalter
        seinerseits wieder als Inhalt behandeln und könnte ihn treffen, läge
        sein Rechteck zufällig im Bereich eines späteren Redact-Annots
        derselben Seite.

        Reine Einfügungen (``repl.start == repl.end``) erzeugen kein
        Redaction-Rechteck — es gibt nichts zu entfernen —, nur eine
        Einfüge-Box für den Text (siehe ``_insertion_rect``). Löschungen
        ohne Ersatztext (``repl.text == ""``) erzeugen umgekehrt ein
        Redaction-Rechteck, aber keinen Stempel.

        **OCR-Segmente** (``location.quelle == "ocr"``) verzweigen vor dem
        Boxen-Nachschlagen: Statt ``rawdict``-Zeichen kommen ihre Boxen aus
        einem **erneuten OCR-Lauf** derselben Seite (``_ocr_zeilen`` —
        dieselben Bytes, dieselbe Engine, dieselbe Sprache, siehe
        Modul-Docstring „OCR läuft dadurch zweimal"). Weil Tesseract nur
        Wort-, keine Zeichen-Boxen liefert, wird dort **wort-granular**
        geschwärzt: Jedes Wort, dessen Zeichenbereich sich mit
        ``[repl.start, repl.end)`` überschneidet, gilt als Treffer und wird
        vollständig redigiert — lieber etwas zu viel überdeckt als ein
        Klarname, der zur Hälfte stehen bleibt (Spec §4.2). Ab hier laufen
        beide Zweige durch dieselben ``redact_rects``/``stamps``-Sammlungen
        und damit auch durch dasselbe „einmal ``apply_redactions()`` je
        Seite" — bei einer Bildseite entfernt das nachweislich auch die
        **Pixel** unter dem Rechteck, nicht nur Text (Spec §4.2).

        **Roundtrip:** Ein leeres ``replacements``-Dict durchläuft trotzdem
        ``doc.tobytes()`` einmal neu — inhaltsgleich, aber nicht
        byte-identisch (Vertrag aus ``adapters/base.py``).
        """
        doc = _open(data)
        try:
            segments = self._build_segments(doc)
            by_id = {segment.id: segment for segment in segments}

            redact_rects: Dict[int, List[pymupdf.Rect]] = {}
            stamps: Dict[int, List[Tuple[pymupdf.Rect, str]]] = {}
            line_cache: Dict[
                int, Dict[int, List[Tuple[str, Tuple[float, float, float, float]]]]
            ] = {}
            ocr_zeilen_cache: Dict[int, List[OcrZeile]] = {}
            ocr_dpi: Optional[int] = None

            for segment_id, repls in replacements.items():
                if not repls:
                    continue
                segment = by_id.get(segment_id)
                if segment is None:
                    continue  # unbekannte ID: laut Vertrag ignorieren
                location: PdfLocation = segment.location

                if location.quelle == "ocr":
                    if ocr_dpi is None:
                        ocr_dpi = self._resolve_ocr_dpi()
                    ocr_zeilen = ocr_zeilen_cache.get(location.page_index)
                    if ocr_zeilen is None:
                        ocr_zeilen = self._ocr_zeilen(doc[location.page_index], ocr_dpi)
                        ocr_zeilen_cache[location.page_index] = ocr_zeilen
                    if location.line_index >= len(ocr_zeilen):
                        continue  # kann bei identischen Bytes nicht vorkommen
                    zeile = ocr_zeilen[location.line_index]

                    for repl in _validate_replacements(list(repls), len(zeile.text)):
                        if repl.start == repl.end:
                            rect = _ocr_insertion_rect(zeile, repl.start, ocr_dpi)
                        else:
                            rect = _ocr_union_rect(
                                zeile, repl.start, repl.end, ocr_dpi
                            )
                            if rect is None:
                                continue  # kein Wort im Bereich: nichts zu tun
                            redact_rects.setdefault(location.page_index, []).append(
                                rect
                            )
                        if repl.text:
                            stamps.setdefault(location.page_index, []).append(
                                (rect, repl.text)
                            )
                    continue

                lines = line_cache.get(location.page_index)
                if lines is None:
                    lines = dict(
                        _iter_page_lines(doc[location.page_index].get_text("rawdict"))
                    )
                    line_cache[location.page_index] = lines
                chars = lines.get(location.line_index)
                if chars is None:
                    continue  # kann bei identischen Bytes nicht vorkommen
                text = _line_text(chars)

                for repl in _validate_replacements(list(repls), len(text)):
                    if repl.start == repl.end:
                        rect = _insertion_rect(chars, repl.start)
                    else:
                        rect = _union_rect(chars, repl.start, repl.end)
                        redact_rects.setdefault(location.page_index, []).append(rect)
                    if repl.text:
                        stamps.setdefault(location.page_index, []).append(
                            (rect, repl.text)
                        )

            for page_index, rects in redact_rects.items():
                page = doc[page_index]
                for rect in rects:
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions()

            for page_index, entries in stamps.items():
                page = doc[page_index]
                for rect, text in entries:
                    _insert_pseudonym(page, rect, text)

            return doc.tobytes()
        finally:
            doc.close()

    # -- Metadaten ---------------------------------------------------------

    def strip_metadata(self, data: bytes) -> bytes:
        """Leert Dokumentinfo (Autor, Titel, …) und die XMP-Metadaten.

        ``doc.set_metadata({})`` leert das klassische Info-Dictionary
        (``/Author``, ``/Title``, ``/Creator``, ``/Producer`` …);
        ``doc.del_xml_metadata()`` entfernt zusätzlich den XMP-Stream, der
        dieselben Angaben redundant als XML mitführt und sonst nach dem
        Info-Dictionary weiterhin den Autor verraten würde.
        """
        doc = _open(data)
        try:
            doc.set_metadata({})
            doc.del_xml_metadata()
            return doc.tobytes()
        finally:
            doc.close()
