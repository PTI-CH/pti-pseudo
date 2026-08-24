"""Gemeinsame Grundlage der Office-Open-XML-Adapter (docx, xlsx, pptx).

Alle drei Formate sind derselbe Behälter: ein ZIP mit XML-Parts, einer
``[Content_Types].xml`` und denselben ``docProps``. Und zwei von ihnen — Word
und PowerPoint — legen Text sogar identisch ab, nämlich als Absatz aus Runs
aus Textknoten. Nur die Namensräume unterscheiden sich (``w:p/w:r/w:t`` gegen
``a:p/a:r/a:t``).

Dieses Modul hält deshalb das, was alle teilen. Es entstand beim Hinzufügen
von xlsx und pptx aus ``docx_adapter``; die Auslagerung war kein Selbstzweck,
sondern die Alternative zu drei Kopien derselben Run-Arithmetik. Genau diese
Arithmetik ist der schwierige Teil eines jeden Adapters: Ein Name steht in
Word regelmässig über mehrere Runs verteilt, weil jemand ein Wort nachträglich
bearbeitet hat, und in PowerPoint aus demselben Grund.

Warum die Bibliotheken hier nicht auftauchen
--------------------------------------------
Weder ``openpyxl`` noch ``python-pptx`` werden gebraucht, und das ist eine
bewusste Entscheidung, keine Sparsamkeit. Beide Bibliotheken **schreiben das
Paket beim Speichern neu**: Diagramme, Pivot-Tabellen, eingebettete Objekte
und unbekannte Parts überleben das nicht zuverlässig. Die Basisklasse sichert
aber zu, dass „Formatierung, Formeln, Bilder, Nummerierung und alle nicht
berührten Teile erhalten bleiben" (siehe ``adapters/base.py``).

:func:`write_package` reicht deshalb jeden nicht angefassten Part
**byte-identisch** durch. Was der Adapter nicht anfasst, kann er auch nicht
kaputt machen — das ist beweisbar statt bloss beabsichtigt. ``python-docx``
ist aus demselben Grund im DOCX-Adapter nur für sprechende Segment-Namen im
Einsatz, nie für den Schreibweg.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from lxml import etree

from backend.adapters.base import Replacement

__all__ = [
    "PACKAGE_NS",
    "PARSER",
    "ParagraphSchema",
    "Span",
    "apply_to_paragraph",
    "make_qn",
    "paragraph_spans",
    "read_package",
    "serialize",
    "strip_docprops",
    "validate_replacements",
    "write_package",
]

#: Namensräume, die jedes OOXML-Paket kennt — unabhängig vom Format.
PACKAGE_NS: Dict[str, str] = {
    "xml": "http://www.w3.org/XML/1998/namespace",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def make_qn(namespaces: Mapping[str, str]):
    """Baut eine ``qn``-Funktion über :data:`PACKAGE_NS` plus Formatnamensräume.

    Jedes Format ergänzt seine eigenen Präfixe (``w`` für Word, ``a``/``p``
    für PowerPoint, ``x`` für Excel) und bekommt eine Funktion, die
    ``"w:t"`` in die Clark-Notation ``"{…main}t"`` übersetzt.

    Examples
    --------
    >>> qn = make_qn({"dc": PACKAGE_NS["dc"]})
    >>> qn("dc:creator")
    '{http://purl.org/dc/elements/1.1/}creator'
    """
    merged = {**PACKAGE_NS, **namespaces}

    def qn(prefixed: str) -> str:
        prefix, _, local = prefixed.partition(":")
        return "{%s}%s" % (merged[prefix], local)

    return qn


#: XXE-sicherer Parser. ``resolve_entities=False`` verhindert, dass ein
#: präpariertes Dokument über eine Entity lokale Dateien einliest.
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

_qn = make_qn({})

CORE_PROPS_PART = "docProps/core.xml"
APP_PROPS_PART = "docProps/app.xml"

#: Felder in ``docProps/core.xml``, die :func:`strip_docprops` leert.
CORE_FIELDS: Tuple[str, ...] = (
    _qn("dc:creator"),
    _qn("cp:lastModifiedBy"),
    _qn("dc:title"),
    _qn("dc:subject"),
    _qn("cp:keywords"),
    _qn("dc:description"),
    _qn("cp:revision"),
)

#: Felder in ``docProps/app.xml``, die :func:`strip_docprops` leert.
APP_FIELDS: Tuple[str, ...] = (
    _qn("ep:Company"),
    _qn("ep:Manager"),
)


# ── ZIP-Ebene ──────────────────────────────────────────────────────────────


def read_package(
    data: bytes, format_name: str
) -> Tuple[List[zipfile.ZipInfo], Dict[str, bytes]]:
    """Liest alle ZIP-Einträge; Reihenfolge und Metadaten bleiben erhalten.

    Parameters
    ----------
    data
        Die vollständigen Bytes des Dokuments.
    format_name
        Wie das Format in Fehlermeldungen heissen soll (``"DOCX"``, ``"XLSX"``,
        ``"PPTX"``). Die Meldung landet in der Oberfläche — „kein gültiges
        XLSX" hilft dort mehr als „kein gültiges OOXML-Paket".

    Raises
    ------
    ValueError
        Wenn die Bytes kein lesbares ZIP sind oder die ``[Content_Types].xml``
        fehlt, die jedes OOXML-Paket tragen muss.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = list(zf.infolist())
            blobs = {info.filename: zf.read(info.filename) for info in infos}
    except zipfile.BadZipFile as exc:
        raise ValueError(
            f"Die Datei ist kein gültiges {format_name} "
            "(kein lesbarer ZIP-Container)."
        ) from exc
    if "[Content_Types].xml" not in blobs:
        raise ValueError(
            f"Die Datei ist kein gültiges {format_name} "
            "([Content_Types].xml fehlt)."
        )
    return infos, blobs


def write_package(
    infos: Iterable[zipfile.ZipInfo],
    blobs: Dict[str, bytes],
    replaced: Dict[str, bytes],
) -> bytes:
    """Packt das Paket neu — Reihenfolge, Zeitstempel und Kompression bleiben.

    Nur die Parts aus ``replaced`` werden ersetzt; alles andere wird
    byte-identisch durchgereicht. Bilder, Themes, Diagramme und Einstellungen
    fasst ein Adapter damit nachweislich nicht an.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in infos:
            payload = replaced.get(info.filename, blobs[info.filename])
            entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            entry.compress_type = info.compress_type
            entry.external_attr = info.external_attr
            entry.internal_attr = info.internal_attr
            entry.create_system = info.create_system
            zf.writestr(entry, payload)
    return buffer.getvalue()


def serialize(root: etree._Element) -> bytes:
    """Serialisiert einen Part so, wie Office es tut (Deklaration, standalone)."""
    return etree.tostring(
        root.getroottree(),
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def parse_part(blobs: Mapping[str, bytes], name: str) -> Optional[etree._Element]:
    """Parst einen Part, oder ``None`` wenn er fehlt bzw. kaputt ist.

    Ein unlesbarer Nebenpart darf das ganze Dokument nicht scheitern lassen —
    er trägt dann eben keine Segmente bei.
    """
    blob = blobs.get(name)
    if blob is None:
        return None
    try:
        return etree.fromstring(blob, PARSER)
    except etree.XMLSyntaxError:
        return None


def strip_docprops(data: bytes, format_name: str) -> bytes:
    """Leert Autor, Firma und verwandte Felder in den ``docProps``.

    Metadaten sind der klassische blinde Fleck: Der Fliesstext ist sauber
    pseudonymisiert, aber ``docProps/core.xml`` nennt weiterhin Autor und
    Firma. Für alle drei OOXML-Formate identisch, weil die ``docProps`` zum
    Paket gehören und nicht zum Format.

    Die Elemente werden **geleert, nicht entfernt** — manche Programme
    erwarten sie und legen sie sonst neu an.

    Nicht angetastet: ``docProps/thumbnail.jpeg`` (das Vorschaubild kann
    Personendaten zeigen; es zu entfernen erfordert auch Anpassungen an
    ``[Content_Types].xml`` und den Beziehungen und gehört in einen eigenen
    Schritt) sowie Autorenangaben an Kommentaren und nachverfolgten
    Änderungen.
    """
    infos, blobs = read_package(data, format_name)
    replaced: Dict[str, bytes] = {}

    for part_name, fields in (
        (CORE_PROPS_PART, CORE_FIELDS),
        (APP_PROPS_PART, APP_FIELDS),
    ):
        root = parse_part(blobs, part_name)
        if root is None:
            continue
        changed = False
        for field_tag in fields:
            for node in root.iter(field_tag):
                if node.text:
                    node.text = ""
                    changed = True
        if changed:
            replaced[part_name] = serialize(root)

    return write_package(infos, blobs, replaced)


# ── Absatz-Ebene: Word und PowerPoint teilen sie ───────────────────────────


@dataclass(frozen=True)
class ParagraphSchema:
    """Die Tag-Namen, mit denen ein Format Absätze, Runs und Text ablegt.

    Word und PowerPoint benutzen dieselbe Struktur in verschiedenen
    Namensräumen. Der Unterschied ist damit Konfiguration, nicht Code.

    Attributes
    ----------
    paragraph
        Tag des Absatzes (``w:p`` bzw. ``a:p``).
    run
        Tag des Runs (``w:r`` bzw. ``a:r``).
    text
        Tag des Textknotens (``w:t`` bzw. ``a:t``).
    separators
        Knoten, die im Fliesstext ein Zeichen erzeugen, aber keinen Text
        tragen — Tabulator und Zeilenumbruch. Sie kommen in den Segmenttext,
        damit die Erkennung nicht ``"Name:Andreas"`` als ein Wort sieht, sind
        aber nicht beschreibbar.
    space_attr
        Attribut, mit dem führender/folgender Leerraum geschützt wird, oder
        ``None``. Word braucht ``xml:space="preserve"``, sonst darf ein
        Verarbeiter Leerzeichen zusammenfalten. DrawingML (PowerPoint) kennt
        das an ``a:t`` nicht und bewahrt Leerraum ohnehin.
    """

    paragraph: str
    run: str
    text: str
    separators: Mapping[str, str]
    space_attr: Optional[str] = None


@dataclass
class Span:
    """Eine Spanne des konkatenierten Absatztexts und ihr XML-Knoten."""

    node: etree._Element
    run: etree._Element
    start: int
    end: int
    #: ``True`` für Textknoten (änderbar), ``False`` für Tabulator/Umbruch.
    writable: bool

    @property
    def length(self) -> int:
        """Ursprüngliche Länge der Spanne im Segmenttext."""
        return self.end - self.start


def runs_of_paragraph(
    paragraph: etree._Element, schema: ParagraphSchema
) -> List[etree._Element]:
    """Sammelt die Runs, die **zu diesem** Absatz gehören — in Lesereihenfolge.

    Nicht einfach ``paragraph.findall(run)``: Runs stecken regelmässig in
    Zwischenelementen — in Word ``w:hyperlink``, ``w:ins``/``w:del``
    (nachverfolgte Änderungen), ``w:smartTag`` und inline ``w:sdt``; in
    PowerPoint ``a:fld`` (Feld) und Hyperlink-Umgebungen. Deshalb wird
    rekursiv abgestiegen.

    Zwei Abbruchbedingungen halten die Segmente überlappungsfrei:

    * Bei einem Run wird **nicht weiter** abgestiegen. Eine Word-Textbox hängt
      als ``w:r/…/w:txbxContent/w:p`` unter einem Run; ihre Absätze sind
      eigene Segmente und würden sonst doppelt erfasst.
    * Ein verschachtelter Absatz wird übersprungen — er ist ein eigenes
      Segment und wird über ``root.iter(paragraph)`` ohnehin gefunden.
    """
    runs: List[etree._Element] = []

    def walk(element: etree._Element) -> None:
        for child in element:
            tag = child.tag
            if not isinstance(tag, str):
                continue  # Kommentare und Processing Instructions
            if tag == schema.run:
                runs.append(child)
            elif tag == schema.paragraph:
                continue
            else:
                walk(child)

    walk(paragraph)
    return runs


def paragraph_spans(
    paragraph: etree._Element, schema: ParagraphSchema
) -> Tuple[str, List[Span]]:
    """Konkateniert den Absatztext und liefert die Landkarte dazu.

    Returns
    -------
    (text, spans)
        ``text`` ist der Segmenttext, ``spans`` die lückenlose, aufsteigend
        sortierte Zuordnung von Offset-Bereichen zu XML-Knoten.
    """
    pieces: List[str] = []
    spans: List[Span] = []
    offset = 0

    for run in runs_of_paragraph(paragraph, schema):
        for node in run:
            tag = node.tag
            if not isinstance(tag, str):
                continue
            if tag == schema.text:
                value = node.text or ""
            elif tag in schema.separators:
                value = schema.separators[tag]
            else:
                # w:drawing, w:rPr, w:delText, a:rPr, a:fld …
                continue
            spans.append(
                Span(
                    node=node,
                    run=run,
                    start=offset,
                    end=offset + len(value),
                    writable=(tag == schema.text),
                )
            )
            pieces.append(value)
            offset += len(value)

    return "".join(pieces), spans


def set_text(node: etree._Element, value: str, schema: ParagraphSchema) -> None:
    """Schreibt Text in einen Textknoten und schützt bei Bedarf den Leerraum.

    Ohne ``xml:space="preserve"`` darf ein XML-Verarbeiter führende und
    folgende Leerzeichen zusammenfalten — Word tut das auch. Aus
    ``"Sehr geehrter [[PERSON_1]] "`` würde sonst still ein fehlendes
    Leerzeichen vor dem nächsten Run.
    """
    node.text = value
    if schema.space_attr and (value[:1].isspace() or value[-1:].isspace()):
        node.set(schema.space_attr, "preserve")


def validate_replacements(
    replacements: List[Replacement], text_length: int
) -> List[Replacement]:
    """Prüft Offsets und Überlappungen; liefert aufsteigend sortiert zurück."""
    ordered = sorted(replacements, key=lambda r: (r.start, r.end))
    previous_end = 0
    for repl in ordered:
        if repl.end > text_length:
            raise ValueError(
                f"Ersetzung {repl.start}–{repl.end} liegt ausserhalb des Segments "
                f"(Länge {text_length})"
            )
        if repl.start < previous_end:
            raise ValueError(
                f"Überlappende Ersetzungen: {repl.start}–{repl.end} überschneidet "
                f"sich mit der vorangehenden, die bis {previous_end} reicht"
            )
        previous_end = repl.end
    return ordered


def _insert(spans: List[Span], repl: Replacement, schema: ParagraphSchema) -> None:
    """Behandelt eine reine Einfügung (``repl.start == repl.end``).

    Eine Einfügung trifft per Definition keine Spanne mit echter Überlappung.
    Die Einfügeposition kann trotzdem mitten in beschreibbarem Text liegen
    (Regelfall) oder exakt an einer Run-Grenze, an der ein nicht
    beschreibbarer Knoten (Tabulator, Umbruch) angrenzt. Im zweiten Fall
    übernimmt der nächstgelegene beschreibbare Run den Text: bevorzugt der
    davor liegende (angehängt), sonst der nächste danach (vorangestellt).

    Neue Runs werden nie angelegt — das würde die zugesicherte Stabilität der
    Absatz-Indizes in den Locations verletzen.
    """
    position = repl.start
    before: Optional[Span] = None
    after: Optional[Span] = None
    for span in spans:
        if not span.writable:
            continue
        if span.end <= position:
            before = span
        elif after is None and span.start >= position:
            after = span

    target = before if before is not None else after
    if target is None:
        raise ValueError(
            f"Für die Einfügung an Position {position} gibt es keinen "
            "beschreibbaren Textknoten im Absatz."
        )

    current = target.node.text or ""
    local = len(current) if target is before else 0
    set_text(target.node, current[:local] + repl.text + current[local:], schema)


def _apply_one(spans: List[Span], repl: Replacement, schema: ParagraphSchema) -> None:
    """Wendet **eine** Ersetzung auf die Run-Landkarte an.

    Der Aufrufer garantiert, dass die Ersetzungen von rechts nach links
    kommen. Nur deshalb dürfen hier die *ursprünglichen* Offsets gegen den
    *aktuellen* Knotentext gerechnet werden: Alle bereits angewendeten
    Ersetzungen lagen weiter rechts, der Textbereich links davon ist unberührt.
    """
    touched = [s for s in spans if s.start < repl.end and s.end > repl.start]

    if not touched:
        # Reine Einfügung (start == end) trifft keine Spanne echt.
        _insert(spans, repl, schema)
        return

    target = next((s for s in touched if s.writable), None)
    if target is None:
        raise ValueError(
            f"Für die Ersetzung {repl.start}–{repl.end} gibt es keinen "
            "beschreibbaren Textknoten im Absatz."
        )

    for span in touched:
        local_start = max(0, repl.start - span.start)
        local_end = min(span.length, repl.end - span.start)

        if not span.writable:
            # Tabulator oder Umbruch liegt im Trefferbereich: Er gehört zum
            # ersetzten Text und verschwindet mit ihm. Sein Elternteil (der
            # Run) bleibt bestehen.
            if local_end > local_start:
                parent = span.node.getparent()
                if parent is not None:
                    parent.remove(span.node)
            continue

        current = span.node.text or ""
        if span is target:
            # Der Ersatztext geht vollständig in den ersten berührten Run —
            # dessen Zeichenformatierung gewinnt für den ganzen Platzhalter.
            set_text(
                span.node,
                current[:local_start] + repl.text + current[local_end:],
                schema,
            )
        else:
            # Weitere berührte Runs verlieren nur ihren überlappenden Teil.
            set_text(span.node, current[:local_start] + current[local_end:], schema)


def apply_to_paragraph(
    paragraph: etree._Element,
    expected_text: str,
    replacements: List[Replacement],
    schema: ParagraphSchema,
) -> None:
    """Wendet alle Ersetzungen eines Segments auf einen Absatz an."""
    text, spans = paragraph_spans(paragraph, schema)
    if text != expected_text:
        raise ValueError(
            "Der Absatz passt nicht mehr zum Segment. Wurden extract_segments() "
            "und apply_replacements() mit denselben Bytes aufgerufen?"
        )
    ordered = validate_replacements(replacements, len(text))
    # Von rechts nach links — sonst verschiebt jede Ersetzung die Offsets der
    # noch ausstehenden um ihre Längendifferenz.
    for repl in reversed(ordered):
        _apply_one(spans, repl, schema)
