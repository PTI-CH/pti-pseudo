"""Registry der Dokument-Adapter.

Ein Aufrufer soll nie entscheiden müssen, *welche* Adapterklasse zuständig ist.
Er kennt entweder den MIME-Typ aus dem Upload (``UploadFile.content_type``)
oder den Dateinamen — beides führt über :func:`get_adapter` zum passenden
Adapter.

Warum beides: Browser schicken für ``.csv`` je nach Betriebssystem
``text/csv``, ``application/vnd.ms-excel`` oder ``application/octet-stream``,
und ein ``curl``-Upload liefert oft gar keinen Typ. Der Dateiname ist im Zweifel
das verlässlichere Signal, deshalb wird er zuerst probiert, wenn die Eingabe wie
ein Dateiname aussieht.

Phase 1 unterstützt DOCX, TXT und CSV. **PDF ist ausdrücklich Phase 5** — ein
PDF formaterhaltend zu verändern heisst, Glyphenpositionen und eingebettete
Schriften neu zu setzen; das ist ein eigenes Teilprojekt und keine Variante des
hier implementierten Segment-Modells. Ein PDF-Upload bekommt deshalb eine
eigene, klare Fehlermeldung statt eines generischen „nicht unterstützt".
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple, Type

from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.adapters.docx_adapter import DocxAdapter, DocxLocation
from backend.adapters.text_adapter import CsvAdapter, TextAdapter

__all__ = [
    "DocumentAdapter",
    "Segment",
    "Replacement",
    "DocxAdapter",
    "DocxLocation",
    "TextAdapter",
    "CsvAdapter",
    "get_adapter",
    "supported_formats",
    "UnsupportedFormatError",
]


class UnsupportedFormatError(ValueError):
    """Das Format wird (noch) nicht unterstützt.

    Erbt von ``ValueError``, damit bestehende Fehlerbehandlung greift, ist aber
    eigenständig, damit die API daraus ein sauberes ``415 Unsupported Media
    Type`` machen kann statt eines ``400``.
    """


#: Reihenfolge = Priorität. Die erste Klasse, die passt, gewinnt.
_ADAPTERS: Tuple[Type[DocumentAdapter], ...] = (DocxAdapter, CsvAdapter, TextAdapter)

#: MIME-Typen, die Browser und Office-Werkzeuge zusätzlich zu den offiziellen
#: für unsere Formate senden. Ohne diese Karte scheitert ein CSV-Upload aus
#: Windows/Excel regelmässig.
_MIME_ALIASES: Dict[str, Type[DocumentAdapter]] = {
    "application/vnd.ms-excel": CsvAdapter,  # Windows meldet das für .csv
    "application/msword": DocxAdapter,  # gelegentlich für .docx gesendet
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ".main+xml": DocxAdapter,
}

#: Formate, die bewusst später kommen — mit Begründung für die Fehlermeldung.
_PLANNED: Dict[str, str] = {
    ".pdf": (
        "PDF wird erst in Phase 5 unterstützt. Formaterhaltendes Ersetzen in PDF "
        "erfordert ein eigenes Textlayout-Verfahren. Bitte das Dokument vorher "
        "als DOCX oder TXT exportieren."
    ),
    ".doc": (
        "Das alte Word-Format (.doc) wird nicht unterstützt. Bitte in Word "
        "einmal als .docx speichern."
    ),
    ".xlsx": (
        "Excel-Dateien (.xlsx) sind für Phase 5 vorgesehen. Bitte das "
        "Tabellenblatt als CSV exportieren."
    ),
    ".rtf": ("RTF wird nicht unterstützt. Bitte in Word einmal als .docx speichern."),
}

_PLANNED_MIME: Dict[str, str] = {
    "application/pdf": _PLANNED[".pdf"],
    "application/msword-doc": _PLANNED[".doc"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _PLANNED[
        ".xlsx"
    ],
    "application/rtf": _PLANNED[".rtf"],
    "text/rtf": _PLANNED[".rtf"],
}


def supported_formats() -> List[str]:
    """Liefert die unterstützten Dateiendungen — für Fehlertexte und die GUI."""
    endings: List[str] = []
    for adapter_class in _ADAPTERS:
        for extension in adapter_class.extensions:
            if extension not in endings:
                endings.append(extension)
    return endings


def _by_extension(extension: str) -> Type[DocumentAdapter] | None:
    """Sucht den Adapter zu einer Dateiendung."""
    for adapter_class in _ADAPTERS:
        if extension in adapter_class.extensions:
            return adapter_class
    return None


def _by_content_type(content_type: str) -> Type[DocumentAdapter] | None:
    """Sucht den Adapter zu einem MIME-Typ."""
    for adapter_class in _ADAPTERS:
        if content_type in adapter_class.content_types:
            return adapter_class
    return _MIME_ALIASES.get(content_type)


def get_adapter(content_type_or_filename: str) -> DocumentAdapter:
    """Liefert den zuständigen Adapter zu MIME-Typ **oder** Dateinamen.

    Parameters
    ----------
    content_type_or_filename
        Entweder ein MIME-Typ (``"text/csv"``, mit oder ohne Parameter wie
        ``"; charset=utf-8"``) oder ein Dateiname bzw. Pfad
        (``"Einvernahme.docx"``). Gross-/Kleinschreibung ist egal.

    Returns
    -------
    DocumentAdapter
        Eine neue, zustandslose Adapter-Instanz.

    Raises
    ------
    UnsupportedFormatError
        Wenn das Format nicht (oder noch nicht) unterstützt wird. Die Meldung
        ist auf Deutsch und für die Anzeige in der Oberfläche gedacht.

    Examples
    --------
    >>> type(get_adapter("Bericht.docx")).__name__
    'DocxAdapter'
    >>> type(get_adapter("text/csv; charset=utf-8")).__name__
    'CsvAdapter'
    """
    if not content_type_or_filename or not content_type_or_filename.strip():
        raise UnsupportedFormatError(
            "Es wurde weder ein Dateiname noch ein Inhaltstyp angegeben — "
            "das Format lässt sich so nicht bestimmen."
        )

    raw = content_type_or_filename.strip()
    # MIME-Parameter abschneiden: "text/csv; charset=utf-8" -> "text/csv"
    normalized = raw.split(";", 1)[0].strip().lower()

    extension = os.path.splitext(normalized)[1]
    looks_like_mime = "/" in normalized and not extension

    if not looks_like_mime and extension:
        planned = _PLANNED.get(extension)
        if planned:
            raise UnsupportedFormatError(planned)
        adapter_class = _by_extension(extension)
        if adapter_class is not None:
            return adapter_class()

    planned = _PLANNED_MIME.get(normalized)
    if planned:
        raise UnsupportedFormatError(planned)

    adapter_class = _by_content_type(normalized)
    if adapter_class is not None:
        return adapter_class()

    # Letzter Versuch: Ein Dateiname mit unbekanntem MIME-Typ davor, oder ein
    # Pfad, dessen Endung erst nach dem MIME-Test greift.
    if extension:
        adapter_class = _by_extension(extension)
        if adapter_class is not None:
            return adapter_class()

    raise UnsupportedFormatError(
        f"Nicht unterstütztes Format: {raw!r}. "
        f"Unterstützt werden in Phase 1: {', '.join(supported_formats())}. "
        "PDF folgt in Phase 5."
    )
