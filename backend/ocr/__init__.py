"""OCR-Kapsel um Tesseract (Spec §4) — wie ``backend/ki/`` Infomaniak kapselt.

Getrennt in zwei Bausteine, aus demselben Grund wie dort: Wer die Engine
wechselt oder anders anspricht, aendert nur :mod:`backend.ocr.engine`;
:mod:`backend.ocr.verfuegbarkeit` (Schalter + Binary-Pruefung) bleibt
unberuehrt, und umgekehrt.

``backend/adapters/pdf_adapter.py`` ist der einzige Aufrufer. Er injiziert
Engine und Verfuegbarkeitspruefung ueber seinen Konstruktor — die hier
exportierten Standardimplementierungen (:class:`TesseractEngine`,
:func:`ocr_verfuegbar`) sind das, was er benutzt, wenn nichts injiziert
wurde.
"""

from backend.ocr.engine import (
    OcrEngine,
    OcrWort,
    OcrZeile,
    TesseractEngine,
    gruppiere_zeilen,
    pixel_zu_punkt,
)
from backend.ocr.verfuegbarkeit import ocr_verfuegbar

__all__ = [
    "OcrEngine",
    "OcrWort",
    "OcrZeile",
    "TesseractEngine",
    "gruppiere_zeilen",
    "ocr_verfuegbar",
    "pixel_zu_punkt",
]
