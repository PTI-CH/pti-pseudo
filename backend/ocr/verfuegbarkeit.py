"""Verfuegbarkeitspruefung fuer die Tesseract-OCR-Stufe (Spec §4.1).

Zwei Bedingungen muessen **beide** zutreffen, bevor der Adapter versucht,
eine gescannte Seite per OCR zu lesen:

1. Der Schalter :attr:`~backend.config.Settings.ocr_enabled` steht auf
   ``True`` — die Deploy-Entscheidung, siehe Kommentar dort.
2. Das Systembinary ``tesseract`` ist auf diesem Knoten tatsaechlich
   auffindbar.

Fehlt eine der beiden, ist OCR nicht verfuegbar — und der Adapter wirft
``NutzermeldungError`` statt eines stillen leeren Ergebnisses (Spec §4.3).

Wie bei :mod:`backend.ki.infomaniak` ist die eigentliche Pruefung
**injizierbar** (``binary_pruefen``), damit sie ohne echtes Tesseract-Binary
getestet werden kann.
"""

from __future__ import annotations

import shutil
from typing import Callable, Optional

__all__ = ["ocr_verfuegbar"]


def _binary_vorhanden() -> bool:
    """Prueft, ob das Systembinary ``tesseract`` lauffaehig ist.

    Zwei Stufen: ``shutil.which`` zuerst — schnell, kein Subprozess —, und
    nur wenn das etwas findet, zusaetzlich ``pytesseract.get_tesseract_version()``
    in einem ``try/except``. Der zweite Schritt faengt den selteneren Fall
    ab, dass unter dem Namen ``tesseract`` etwas anderes im ``PATH`` liegt
    oder das Binary kaputt ist — ``shutil.which`` allein prueft nur, dass
    eine ausfuehrbare Datei mit diesem Namen existiert, nicht dass sie
    tatsaechlich Tesseract ist.
    """
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — jeder Ausfall bedeutet "nicht verfuegbar"
        return False
    return True


def ocr_verfuegbar(
    einstellungen=None,
    *,
    binary_pruefen: Optional[Callable[[], bool]] = None,
) -> bool:
    """Ob OCR jetzt laufen kann — Schalter AN **und** Binary auffindbar.

    Parameters
    ----------
    einstellungen
        Ein :class:`~backend.config.Settings`-Objekt (oder ein Test-Double
        mit einem ``ocr_enabled``-Attribut). ``None`` laedt die echten,
        prozessweiten Einstellungen ueber ``get_settings()`` — der Import
        geschieht lokal, damit dieses Modul auch ohne geladene Anwendung
        importierbar bleibt (dasselbe Muster wie in
        ``backend/ki/infomaniak.py``).
    binary_pruefen
        Ersetzt die echte Binary-Pruefung. Nur so laesst sich „Schalter an,
        Binary fehlt" und „Schalter an, Binary da" ohne echtes Tesseract auf
        dem Testrechner unterscheiden.

    Returns
    -------
    bool
        ``True`` nur, wenn beide Bedingungen erfuellt sind.
    """
    if einstellungen is None:
        from backend.config import get_settings

        einstellungen = get_settings()

    if not einstellungen.ocr_enabled:
        return False

    pruefen = binary_pruefen or _binary_vorhanden
    return pruefen()
