r"""Baut minimale, gültige Text-PDFs für die PDF-Adaptertests.

Analog zu ``ooxml_fabrik.py`` (siehe dessen Modulkopf zur Begründung „von
Hand statt Bibliothek"): Diese Fabrik erzeugt **ausschliesslich Textebene**
— keine Bildseiten. Gescannte (bildbasierte) PDFs gehören zur OCR-Stufe
(Spec §4) und damit zum parallelen Arbeitsstrang; dieses Modul rührt sie
nicht an.

``page.insert_text`` statt ``page.insert_textbox``
----------------------------------------------------
``page.insert_textbox`` mit einem mehrzeiligen Puffer (``"a\\nb"``) wäre die
naheliegende Abkürzung, verschluckt aber eine reine Leerzeile ersatzlos: Für
``"a\\n\\nb"`` legt ``rawdict`` **keinen** Zeileneintrag für die leere Zeile
an, sie verschwindet spurlos zwischen den beiden Textzeilen. Für den
Segmentierungstest (Spec §7.5 — „leere Zeilen erzeugen keine Segmente")
braucht es aber eine Zeile, die im ``rawdict`` tatsächlich **ankommt** und
erst der Adapter verwirft (``text.strip()``), nicht eine, die schon
PyMuPDF nie erzeugt.

Diese Fabrik setzt deshalb jede Zeile einzeln über ``page.insert_text`` an
eine eigene, um ``_ZEILENHOEHE`` versetzte Position. Das ergibt volle
Kontrolle: ein leerer String (``""``) erzeugt gar keinen Aufruf und damit
gar keine Zeile (wie eine ausgelassene Zeile im Layout); ein String aus
reinem Leerraum (``"   "``) erzeugt einen echten ``rawdict``-Zeileneintrag
mit Leerraum-Zeichen, den erst der Adapter verwirft. Beide Fälle zusammen
decken die Segmentierungsregel ab.

Alle Namen sind frei erfunden (Hausregel: keine echten Personendaten im
Repository) und mit echten Umlauten geschrieben — das deutsche spaCy-Modell
hält eine transliterierte Form wie „Spaeter" fälschlich für einen
Personennamen, „Später" nicht (siehe ``golden-set/README.md``).
"""

from __future__ import annotations

from typing import Sequence

import pymupdf

__all__ = ["baue_pdf", "setze_metadaten"]

#: Linker Rand für jede eingesetzte Zeile.
_RAND_X = 36.0

#: Vertikaler Abstand aufeinanderfolgender Zeilen.
_ZEILENHOEHE = 14.0

#: Erste Zeilen-Baseline von oben.
_START_Y = 60.0

#: Basis-14-Schriftart — dieselbe wie im Adapter selbst (``_FONT`` in
#: ``pdf_adapter.py``), damit die Testdokumente kein Font-Embedding brauchen.
_FONT = "helv"

_FONTSIZE = 11.0


def baue_pdf(seiten: Sequence[Sequence[str]], *, fontsize: float = _FONTSIZE) -> bytes:
    """Baut ein PDF mit reiner Textebene aus Seiten von Zeilen.

    Parameters
    ----------
    seiten
        Je Seite eine Folge von Zeilentexten, in der Reihenfolge, in der sie
        auf der Seite erscheinen sollen. Ein leerer String (``""``) erzeugt
        keine Zeile; ein String aus reinem Leerraum erzeugt eine Zeile, die
        der Adapter als leer verwirft (siehe Modulkopf).
    fontsize
        Schriftgrösse für alle Zeilen aller Seiten dieses Aufrufs.

    Returns
    -------
    bytes
        Die fertigen PDF-Bytes.
    """
    doc = pymupdf.open()
    try:
        for zeilen in seiten:
            page = doc.new_page()
            y = _START_Y
            for zeile in zeilen:
                if zeile:
                    page.insert_text(
                        (_RAND_X, y), zeile, fontsize=fontsize, fontname=_FONT
                    )
                y += _ZEILENHOEHE
        return doc.tobytes()
    finally:
        doc.close()


def setze_metadaten(data: bytes, *, autor: str = "", titel: str = "") -> bytes:
    """Setzt Autor und Titel im klassischen Info-Dictionary eines PDF.

    Für den Metadaten-Test (Spec §7.3): ``PdfAdapter.strip_metadata`` muss
    diese Felder anschliessend leeren.

    Parameters
    ----------
    data
        PDF-Bytes, z. B. das Ergebnis von :func:`baue_pdf`.
    autor, titel
        Werte für ``/Author`` bzw. ``/Title``.

    Returns
    -------
    bytes
        Das PDF mit gesetzten Metadaten.
    """
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        doc.set_metadata({"author": autor, "title": titel})
        return doc.tobytes()
    finally:
        doc.close()
