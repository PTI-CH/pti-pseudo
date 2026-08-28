"""Engine-Abstraktion um Tesseract — Wort-Boxen statt Zeichen-Boxen.

``page.get_text("rawdict")`` (Textebene, siehe ``pdf_adapter.py``) liefert
Glyphen-genaue Boxen; Tesseract liefert nur **Wort**-Boxen
(``pytesseract.image_to_data``). Diese Kornung bestimmt, wie der OCR-Zweig
von ``PdfAdapter.apply_replacements`` redigieren muss: nicht zeichen-, nur
wort-granular (siehe Modul-Docstring dort).

Die Engine ist **injizierbar** — genau wie der HTTP-Client in
``backend/ki/infomaniak.py`` — damit sich die Logik rund um Gruppierung,
Umrechnung und Redaction ohne ein echtes Tesseract-Binary testen laesst. Ein
Test setzt eine Fake-Engine ein, die kanonische Wort-Boxen zurueckgibt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, List, Protocol, Tuple

if TYPE_CHECKING:
    from PIL.Image import Image

__all__ = [
    "OcrEngine",
    "OcrWort",
    "OcrZeile",
    "TesseractEngine",
    "gruppiere_zeilen",
    "pixel_zu_punkt",
]


@dataclass(frozen=True)
class OcrWort:
    """Ein von der Engine erkanntes Wort mit seiner Pixel-Bounding-Box.

    Attributes
    ----------
    text
        Der erkannte Wortlaut.
    left, top, width, height
        Bounding-Box in **Pixeln des gerasterten Bildes** — nicht in
        PDF-Punkten. Die Umrechnung uebernimmt :func:`pixel_zu_punkt`, mit
        dem Rasterungs-Zoom (dpi), der bei der Erkennung benutzt wurde.
    block_num, par_num, line_num, word_num
        Tesseracts eigene Gliederung. Die Kombination ``(block_num,
        par_num, line_num)`` identifiziert eine „Zeile" eindeutig — jedes
        dieser Felder allein wiederholt sich (``line_num`` faengt in jedem
        Absatz wieder bei 1 an); ``word_num`` ordnet die Woerter innerhalb
        einer Zeile.
    """

    text: str
    left: int
    top: int
    width: int
    height: int
    block_num: int
    par_num: int
    line_num: int
    word_num: int


@dataclass(frozen=True)
class OcrZeile:
    """Eine aus Woertern zusammengesetzte OCR-„Zeile" — das OCR-Segment.

    Entspricht der Rolle, die eine ``rawdict``-Zeile in der Textebene
    spielt (siehe ``pdf_adapter._iter_page_lines``): Die natuerliche
    Segment-Einheit, klein genug fuer eine eindeutige Stelle.

    Attributes
    ----------
    line_key
        ``(block_num, par_num, line_num)`` der zusammengefassten Woerter —
        nur zu Diagnosezwecken mitgefuehrt, fuer die Adressierung ist der
        Index dieser Zeile in der von :func:`gruppiere_zeilen` gelieferten
        Liste massgeblich (siehe ``PdfLocation.line_index``).
    text
        Die Woerter, durch ein einzelnes Leerzeichen getrennt, in
        ``word_num``-Reihenfolge — das ``Segment.text`` dieser Zeile.
    wort_spannen
        Fuer jedes Wort das ``(start, end, wort)``-Tripel: sein
        Zeichenbereich **in** ``text`` (halboffen, wie
        ``Replacement.start``/``.end``). Damit findet der Redaction-Zweig in
        ``apply_replacements`` fuer eine gegebene Ersetzung ``[start, end)``
        alle Woerter, deren Bereich sich damit ueberschneidet.
    """

    line_key: Tuple[int, int, int]
    text: str
    wort_spannen: Tuple[Tuple[int, int, OcrWort], ...]


def gruppiere_zeilen(woerter: Iterable[OcrWort]) -> List[OcrZeile]:
    """Fasst Woerter zu Zeilen zusammen und baut ihre Zeichenbereiche.

    Die Gruppierung haengt an ``(block_num, par_num, line_num)`` — nicht an
    ``line_num`` allein, denn Tesseract zaehlt Zeilennummern je Absatz neu
    (siehe :class:`OcrWort`). Die Reihenfolge der Zeilen in der Rueckgabe
    ist die Reihenfolge, in der ihre Schluessel zum ersten Mal auftauchen —
    bei Tesseracts TSV-Ausgabe ist das die Leserichtung der Seite. Innerhalb
    einer Zeile werden die Woerter nach ``word_num`` sortiert, unabhaengig
    von der Reihenfolge in ``woerter``.

    ``woerter`` enthaelt laut :class:`OcrWort`-Vertrag nie leere oder reine
    Whitespace-Texte (das filtert schon die Engine, siehe
    :meth:`TesseractEngine.erkenne_seite`) — jede zurueckgegebene
    :class:`OcrZeile` hat deshalb garantiert nicht-leeren Text, anders als
    bei der Textebene gibt es hier also keine leeren Zeilen zu ueberspringen.

    Parameters
    ----------
    woerter
        Die von einer :class:`OcrEngine` erkannten Woerter einer Seite.

    Returns
    -------
    list of OcrZeile
        Eine Zeile je ``(block_num, par_num, line_num)``-Gruppe.
    """
    gruppen: dict[Tuple[int, int, int], List[OcrWort]] = {}
    reihenfolge: List[Tuple[int, int, int]] = []
    for wort in woerter:
        schluessel = (wort.block_num, wort.par_num, wort.line_num)
        if schluessel not in gruppen:
            gruppen[schluessel] = []
            reihenfolge.append(schluessel)
        gruppen[schluessel].append(wort)

    zeilen: List[OcrZeile] = []
    for schluessel in reihenfolge:
        geordnet = sorted(gruppen[schluessel], key=lambda w: w.word_num)
        teile: List[str] = []
        spannen: List[Tuple[int, int, OcrWort]] = []
        position = 0
        for index, wort in enumerate(geordnet):
            if index > 0:
                teile.append(" ")
                position += 1
            start = position
            teile.append(wort.text)
            position += len(wort.text)
            spannen.append((start, position, wort))
        zeilen.append(OcrZeile(schluessel, "".join(teile), tuple(spannen)))
    return zeilen


def pixel_zu_punkt(x_px: float, y_px: float, dpi: int) -> Tuple[float, float]:
    """Rechnet eine Pixel-Koordinate des gerasterten Bildes in PDF-Punkte um.

    PDF misst in Punkten (1/72 Zoll); das Bild wurde mit ``dpi`` Pixeln je
    Zoll gerastert (``page.get_pixmap(dpi=dpi)``). Eine Pixelkoordinate ist
    deshalb mit dem Rasterungs-Zoom ``dpi / 72`` skaliert; die Umkehrung
    dieser Skalierung ist die gesuchte Punktkoordinate (Spec §4.2).

    Es wird angenommen, dass die Seite bei ``(0, 0)`` beginnt — zutreffend
    fuer die ueblichen, nicht manuell zugeschnittenen PDF-Seiten, die
    ``page.get_pixmap()`` ohne ``clip`` rastert.

    Parameters
    ----------
    x_px, y_px
        Pixelkoordinate im gerasterten Bild.
    dpi
        Die beim Rastern benutzte Aufloesung
        (:attr:`~backend.config.Settings.ocr_dpi`).

    Returns
    -------
    tuple of float
        ``(x_pt, y_pt)`` in PDF-Punkten.
    """
    zoom = dpi / 72.0
    return x_px / zoom, y_px / zoom


class OcrEngine(Protocol):
    """Das Wenige, das ``PdfAdapter`` von einer OCR-Engine braucht."""

    def erkenne_seite(self, bild: "Image", *, sprache: str = "deu") -> List[OcrWort]:
        """Erkennt Text auf einem gerasterten Seitenbild.

        Parameters
        ----------
        bild
            Die gerasterte Seite als ``PIL.Image.Image``
            (``pymupdf.Pixmap.pil_image()``).
        sprache
            Tesseract-Sprachcode. ``"deu"`` in dieser Anwendung durchgehend.

        Returns
        -------
        list of OcrWort
            Nur tatsaechliche Woerter — leere oder reine Whitespace-Treffer
            (Tesseracts Zeilen-/Block-/Absatz-Zeilen der TSV-Ausgabe ohne
            Text) sind bereits herausgefiltert.
        """
        ...


class TesseractEngine:
    """Ruft das Systembinary ``tesseract`` ueber ``pytesseract`` auf.

    Wird nur verwendet, wenn :func:`backend.ocr.ocr_verfuegbar` zuvor
    bestaetigt hat, dass das Binary auf dem Knoten liegt — ohne diese
    Vorpruefung wirft jeder Aufruf
    ``pytesseract.TesseractNotFoundError``.
    """

    def erkenne_seite(self, bild: "Image", *, sprache: str = "deu") -> List[OcrWort]:
        """Siehe :meth:`OcrEngine.erkenne_seite`.

        Ruft ``pytesseract.image_to_data(..., output_type=Output.DICT)`` —
        das liefert ein Dict paralleler Listen (eine TSV-Zeile je Index),
        mit Spalten ``level``, ``block_num``, ``par_num``, ``line_num``,
        ``word_num``, ``left``, ``top``, ``width``, ``height``, ``conf`` und
        ``text``. Nicht-Wort-Zeilen der TSV-Ausgabe (Seiten-, Block-,
        Absatz- und Zeilensummen) tragen dort einen leeren ``text`` — sie
        werden hier verworfen, ohne extra auf ``level`` zu pruefen.
        """
        import pytesseract

        daten = pytesseract.image_to_data(
            bild, lang=sprache, output_type=pytesseract.Output.DICT
        )
        anzahl = len(daten.get("text", []))
        woerter: List[OcrWort] = []
        for index in range(anzahl):
            text = daten["text"][index]
            if not text or not text.strip():
                continue
            woerter.append(
                OcrWort(
                    text=text,
                    left=int(daten["left"][index]),
                    top=int(daten["top"][index]),
                    width=int(daten["width"][index]),
                    height=int(daten["height"][index]),
                    block_num=int(daten["block_num"][index]),
                    par_num=int(daten["par_num"][index]),
                    line_num=int(daten["line_num"][index]),
                    word_num=int(daten["word_num"][index]),
                )
            )
        return woerter
