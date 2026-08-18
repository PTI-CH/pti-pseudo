"""Adapter für Word-Dokumente (``.docx``, OOXML/WordprocessingML).

Ein ``.docx`` ist ein ZIP-Container voller XML-Parts. Der Fliesstext liegt in
``word/document.xml``, aber eben nicht nur dort. Dieser Adapter löst die zwei
Probleme, an denen naive Word-Pseudonymisierer scheitern.

Problem 1 — Word zerlegt Sätze willkürlich in Runs
--------------------------------------------------
Der sichtbare Text eines Absatzes steht in ``w:t``-Knoten innerhalb von
``w:r``-Runs. Wo Word einen Run beendet, hat mit dem Satzbau nichts zu tun:
Rechtschreibprüfung (``w:proofErr``), eine geänderte Zeichenformatierung, ein
Sprachwechsel, nachverfolgte Änderungen (``w:ins``/``w:del``) oder schlicht die
Bearbeitungshistorie erzeugen Schnitte mitten im Wort. „Andreas Meier" kann als
``["Andreas ", "Mei", "er"]`` im XML stehen — dreimal derselbe Name, dreimal
kein Treffer, wenn man Run für Run analysiert.

Lösung: Pro Absatz werden die Run-Texte **konkateniert**; parallel entsteht eine
Landkarte aus Spannen ``(Knoten, Start-Offset, End-Offset)``. Die Erkennung
läuft auf dem konkatenierten String — das ist das
:class:`~backend.adapters.base.Segment`.
Beim Zurückschreiben gilt:

* Ersetzungen werden **von rechts nach links** angewendet. Sonst verschiebt die
  erste Ersetzung alle folgenden Offsets um ihre Längendifferenz.
* Der Ersatztext geht **vollständig in den ersten berührten Run**; dessen
  Formatierung gewinnt damit für den gesamten Platzhalter. Die überlappenden
  Teile der weiteren berührten Runs werden entfernt.
* Runs, die dadurch leer werden, **bleiben als leere Runs stehen**. Sie zu
  löschen ist verlockend, bricht aber Word-interne Verweise: Lesezeichen,
  Kommentar-Anker, Verweise aus ``w:rPr`` sowie die Bereichsgrenzen von
  nachverfolgten Änderungen zeigen auf Positionen zwischen Runs. Ein leerer Run
  ist unsichtbar und harmlos.

Problem 2 — ``python-docx`` erreicht nicht alles
------------------------------------------------
``python-docx`` exponiert Body-Absätze, Tabellen sowie ``section.header``
und ``section.footer``. Es exponiert **nicht**:

* Fussnoten (``word/footnotes.xml``) und Endnoten (``word/endnotes.xml``)
* Kommentare (``word/comments.xml``) samt ihren Erweiterungen
* Textfelder/Textboxen (``w:txbxContent``, verschachtelt in einem Run)
* Blockweise Content-Controls (``w:sdt``) im Body
* den Glossar-Part (``word/glossary/document.xml``, Schnellbausteine)

Genau dort bleiben Personendaten unbemerkt stehen — der Fliesstext ist sauber,
die Fussnote nennt weiter den Klarnamen. Deshalb arbeitet dieser Adapter
**generisch über lxml auf allen Parts des Pakets**: Jeder XML-Part im
WordprocessingML-Namensraum wird nach ``w:p`` durchsucht, und auf jeden Absatz
läuft dieselbe Run-Logik. ``python-docx`` wird nur noch dazu benutzt, die
Absätze zu *benennen*, die es selbst erreicht (``body/p3``,
``body/t0/r1/c2/p0``, ``header0/p0``). Alles Übrige bekommt eine ID mit dem
Präfix ``fallback:`` — an der ID ist damit ablesbar, welche Fundstelle nur dank
des lxml-Wegs sichtbar wurde.

Diese Umkehrung (lxml führt, ``python-docx`` beschriftet) ist Absicht: Ein
zweigleisiges Vorgehen — hier ``python-docx``, dort lxml — müsste die
Doppelerfassung eines Absatzes verhindern, sonst würde dieselbe Stelle zweimal
ersetzt und die zweite Ersetzung liefe auf bereits verändertem Text. Über einen
einzigen Durchlauf ist Überlappungsfreiheit strukturell garantiert.

Nicht abgedeckt (bewusst, siehe auch :meth:`DocxAdapter.strip_metadata`)
-----------------------------------------------------------------------
* ``w:delText`` — gelöschter Text aus nachverfolgten Änderungen. Er ist nicht
  sichtbar und würde den Segmenttext mit Wortresten durchsetzen; er steckt aber
  noch in der Datei. Änderungen vorher annehmen.
* ``w:instrText`` — Feldbefehle (``HYPERLINK "mailto:…"``, ``AUTHOR``).
* ``w:author``-Attribute an Kommentaren und nachverfolgten Änderungen.
* Eingebettete Objekte (OLE, eingebettete Tabellen als Fremdformat) und Bilder.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from docx import Document
from lxml import etree

from backend.adapters.base import DocumentAdapter, Replacement, Segment

__all__ = ["DocxAdapter", "DocxLocation"]

# ── Namensräume ────────────────────────────────────────────────────────────
_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "xml": "http://www.w3.org/XML/1998/namespace",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
}


def qn(prefixed: str) -> str:
    """Wandelt ``"w:t"`` in den Clark-Notation-Tag ``"{…main}t"``."""
    prefix, _, local = prefixed.partition(":")
    return "{%s}%s" % (_NS[prefix], local)


_W_NS_BRACED = "{%s}" % _NS["w"]
_PARA_TAG = qn("w:p")
_RUN_TAG = qn("w:r")
_TEXT_TAG = qn("w:t")
_XML_SPACE = qn("xml:space")

#: Knoten, die im Fliesstext eines Runs ein Zeichen erzeugen, aber keinen Text
#: tragen. Sie kommen in den Segmenttext, damit die Erkennung nicht
#: „Name:Andreas" als ein Wort sieht — sind aber nicht beschreibbar.
_SEPARATOR_TAGS = {
    qn("w:tab"): "\t",
    qn("w:br"): "\n",
    qn("w:cr"): "\n",
}

#: XXE-sicherer Parser. ``resolve_entities=False`` verhindert, dass ein
#: präpariertes Dokument über eine Entity lokale Dateien einliest.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

#: Der Haupt-Part kommt in der Segmentreihenfolge immer zuerst.
_MAIN_PART = "word/document.xml"

_CORE_PROPS_PART = "docProps/core.xml"
_APP_PROPS_PART = "docProps/app.xml"

#: Felder in ``docProps/core.xml``, die geleert werden.
_CORE_FIELDS = (
    qn("dc:creator"),
    qn("cp:lastModifiedBy"),
    qn("dc:title"),
    qn("dc:subject"),
    qn("cp:keywords"),
    qn("dc:description"),
    qn("cp:revision"),
)

#: Felder in ``docProps/app.xml``, die geleert werden.
_APP_FIELDS = (
    qn("ep:Company"),
    qn("ep:Manager"),
)


@dataclass(frozen=True)
class DocxLocation:
    """Adresse eines Absatzes im Paket — für Aufrufer opak.

    Bewusst nur primitive Werte, keine Element-Referenz: Zwischen
    ``extract_segments()`` und ``apply_replacements()`` liegt möglicherweise ein
    anderer HTTP-Request, und das Paket wird dann neu geparst.

    Attributes
    ----------
    part_name
        Name des ZIP-Eintrags, z. B. ``"word/document.xml"``.
    paragraph_index
        0-basierter Index des ``w:p`` in Dokumentreihenfolge **innerhalb dieses
        Parts** (``root.iter("w:p")``). Der Index ist stabil, weil der Adapter
        beim Ersetzen niemals Absätze oder Runs hinzufügt oder entfernt.
    """

    part_name: str
    paragraph_index: int


@dataclass
class _Span:
    """Eine Spanne des konkatenierten Absatztexts und ihr XML-Knoten."""

    node: etree._Element
    run: etree._Element
    start: int
    end: int
    #: ``True`` für ``w:t`` (Text änderbar), ``False`` für ``w:tab``/``w:br``.
    writable: bool

    @property
    def length(self) -> int:
        """Ursprüngliche Länge der Spanne im Segmenttext."""
        return self.end - self.start


# ── ZIP-Ebene ──────────────────────────────────────────────────────────────


def _read_package(data: bytes) -> Tuple[List[zipfile.ZipInfo], Dict[str, bytes]]:
    """Liest alle ZIP-Einträge; Reihenfolge und Metadaten bleiben erhalten."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = list(zf.infolist())
            blobs = {info.filename: zf.read(info.filename) for info in infos}
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "Die Datei ist kein gültiges DOCX (kein lesbarer ZIP-Container)."
        ) from exc
    if "[Content_Types].xml" not in blobs:
        raise ValueError(
            "Die Datei ist kein gültiges DOCX ([Content_Types].xml fehlt)."
        )
    return infos, blobs


def _write_package(
    infos: Iterable[zipfile.ZipInfo],
    blobs: Dict[str, bytes],
    replaced: Dict[str, bytes],
) -> bytes:
    """Packt das Paket neu — Reihenfolge, Zeitstempel und Kompression bleiben.

    Nur die Parts aus ``replaced`` werden ersetzt; alles andere wird
    byte-identisch durchgereicht. Bilder, Themes und Einstellungen fasst der
    Adapter damit nachweislich nicht an.
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


def _serialize(root: etree._Element) -> bytes:
    """Serialisiert einen Part so, wie Word es tut (Deklaration, standalone)."""
    return etree.tostring(
        root.getroottree(),
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


class _DocxPackage:
    """Das entpackte Paket mit allen WordprocessingML-Parts.

    Hält die geparsten lxml-Bäume, damit Extraktion und Ersetzung auf
    **denselben** Objekten arbeiten können.
    """

    def __init__(self, data: bytes) -> None:
        self.infos, self.blobs = _read_package(data)
        self.roots: Dict[str, etree._Element] = {}
        self.paragraphs: Dict[str, List[etree._Element]] = {}
        self.modified: set[str] = set()

        for name in self._ordered_names():
            root = self._parse_wordprocessing(self.blobs[name])
            if root is None:
                continue
            paragraphs = list(root.iter(_PARA_TAG))
            if not paragraphs:
                # styles.xml, settings.xml, fontTable.xml … tragen keinen Text.
                continue
            self.roots[name] = root
            self.paragraphs[name] = paragraphs

    def _ordered_names(self) -> List[str]:
        """Part-Namen in stabiler Reihenfolge: Hauptdokument zuerst."""
        names = sorted(n for n in self.blobs if n.lower().endswith(".xml"))
        if _MAIN_PART in names:
            names.remove(_MAIN_PART)
            names.insert(0, _MAIN_PART)
        return names

    @staticmethod
    def _parse_wordprocessing(blob: bytes) -> Optional[etree._Element]:
        """Parst einen Part; ``None``, wenn er nicht zu WordprocessingML gehört."""
        try:
            root = etree.fromstring(blob, _PARSER)
        except etree.XMLSyntaxError:
            return None
        if not isinstance(root.tag, str) or not root.tag.startswith(_W_NS_BRACED):
            return None
        return root

    def to_bytes(self) -> bytes:
        """Serialisiert die geänderten Parts und packt das Paket neu."""
        replaced = {name: _serialize(self.roots[name]) for name in self.modified}
        return _write_package(self.infos, self.blobs, replaced)


# ── Run-Landkarte ──────────────────────────────────────────────────────────


def _runs_of_paragraph(paragraph: etree._Element) -> List[etree._Element]:
    """Sammelt die Runs, die **zu diesem** Absatz gehören — in Lesereihenfolge.

    Nicht einfach ``paragraph.findall("w:r")``: Runs stecken regelmässig in
    Zwischenelementen — ``w:hyperlink``, ``w:ins``/``w:del`` (nachverfolgte
    Änderungen), ``w:smartTag``, ``w:bookmarkStart``-Umgebungen und inline
    ``w:sdt`` (Content-Controls). Deshalb wird rekursiv abgestiegen.

    Zwei Abbruchbedingungen halten die Segmente überlappungsfrei:

    * Bei einem ``w:r`` wird **nicht weiter** abgestiegen. Eine Textbox hängt
      als ``w:r/…/w:txbxContent/w:p`` unter einem Run; ihre Absätze sind eigene
      Segmente und würden sonst doppelt erfasst.
    * Ein verschachteltes ``w:p`` wird übersprungen — es ist ein eigenes
      Segment und wird über ``root.iter("w:p")`` ohnehin gefunden.
    """
    runs: List[etree._Element] = []

    def walk(element: etree._Element) -> None:
        for child in element:
            tag = child.tag
            if not isinstance(tag, str):
                continue  # Kommentare und Processing Instructions
            if tag == _RUN_TAG:
                runs.append(child)
            elif tag == _PARA_TAG:
                continue
            else:
                walk(child)

    walk(paragraph)
    return runs


def _paragraph_spans(paragraph: etree._Element) -> Tuple[str, List[_Span]]:
    """Konkateniert den Absatztext und liefert die Landkarte dazu.

    Returns
    -------
    (text, spans)
        ``text`` ist der Segmenttext, ``spans`` die lückenlose, aufsteigend
        sortierte Zuordnung von Offset-Bereichen zu XML-Knoten.
    """
    pieces: List[str] = []
    spans: List[_Span] = []
    offset = 0

    for run in _runs_of_paragraph(paragraph):
        for node in run:
            tag = node.tag
            if not isinstance(tag, str):
                continue
            if tag == _TEXT_TAG:
                value = node.text or ""
            elif tag in _SEPARATOR_TAGS:
                value = _SEPARATOR_TAGS[tag]
            else:
                # w:drawing, w:rPr, w:delText, w:instrText, w:footnoteReference …
                continue
            spans.append(
                _Span(
                    node=node,
                    run=run,
                    start=offset,
                    end=offset + len(value),
                    writable=(tag == _TEXT_TAG),
                )
            )
            pieces.append(value)
            offset += len(value)

    return "".join(pieces), spans


def _set_text(node: etree._Element, value: str) -> None:
    """Schreibt Text in einen ``w:t`` und setzt bei Bedarf ``xml:space``.

    Ohne ``xml:space="preserve"`` darf ein XML-Verarbeiter führende und
    folgende Leerzeichen zusammenfalten — Word tut das auch. Aus
    ``"Sehr geehrter [[PERSON_1]] "`` würde sonst still ein fehlendes
    Leerzeichen vor dem nächsten Run.
    """
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(_XML_SPACE, "preserve")


def _validate(replacements: List[Replacement], text_length: int) -> List[Replacement]:
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


def _insert(spans: List[_Span], repl: Replacement) -> None:
    """Behandelt eine reine Einfügung (``repl.start == repl.end``).

    Eine Einfügung trifft per Definition keine Spanne mit echter Überlappung
    (``s.start < end and s.end > start`` kann für ``start == end`` nie zwei
    verschiedene Spannen echt schneiden). Die Einfügeposition kann trotzdem
    mitten in beschreibbarem Text liegen (Regelfall: die nächste — oder bei
    einer Absatzmitte die passende — Spanne bekommt den Text) oder exakt an
    einer Run-Grenze, an der ein nicht beschreibbarer Knoten (``w:tab``,
    ``w:br``/``w:cr``) angrenzt. Im zweiten Fall gäbe es early-return-frei
    *keinen* Knoten, dessen ``[start, end]``-Bereich die Position exakt
    trifft — der nächstgelegene beschreibbare Run übernimmt den Text
    trotzdem: bevorzugt der davor liegende (angehängt), sonst der nächste
    danach (vorangestellt). Neue Runs werden nie angelegt — das würde die in
    :class:`DocxLocation` zugesicherte Stabilität der Absatz-Indizes verletzen.
    """
    position = repl.start
    before: Optional[_Span] = None
    after: Optional[_Span] = None
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
            f"beschreibbaren w:t-Knoten im Absatz."
        )

    current = target.node.text or ""
    local = len(current) if target is before else 0
    _set_text(target.node, current[:local] + repl.text + current[local:])


def _apply_one(spans: List[_Span], repl: Replacement) -> None:
    """Wendet **eine** Ersetzung auf die Run-Landkarte an.

    Der Aufrufer garantiert, dass die Ersetzungen von rechts nach links
    kommen. Nur deshalb dürfen hier die *ursprünglichen* Offsets gegen den
    *aktuellen* Knotentext gerechnet werden: Alle bereits angewendeten
    Ersetzungen lagen weiter rechts, der Textbereich links davon ist unberührt.
    """
    touched = [s for s in spans if s.start < repl.end and s.end > repl.start]

    if not touched:
        # Reine Einfügung (start == end) trifft keine Spanne echt.
        _insert(spans, repl)
        return

    target = next((s for s in touched if s.writable), None)
    if target is None:
        raise ValueError(
            f"Für die Ersetzung {repl.start}–{repl.end} gibt es keinen "
            f"beschreibbaren w:t-Knoten im Absatz."
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
            _set_text(
                span.node, current[:local_start] + repl.text + current[local_end:]
            )
        else:
            # Weitere berührte Runs verlieren nur ihren überlappenden Teil.
            _set_text(span.node, current[:local_start] + current[local_end:])


def _apply_to_paragraph(
    paragraph: etree._Element,
    expected_text: str,
    replacements: List[Replacement],
) -> None:
    """Wendet alle Ersetzungen eines Segments auf einen Absatz an."""
    text, spans = _paragraph_spans(paragraph)
    if text != expected_text:
        raise ValueError(
            "Der Absatz passt nicht mehr zum Segment. Wurden extract_segments() "
            "und apply_replacements() mit denselben Bytes aufgerufen?"
        )
    ordered = _validate(replacements, len(text))
    # Von rechts nach links — sonst verschiebt jede Ersetzung die Offsets der
    # noch ausstehenden um ihre Längendifferenz.
    for repl in reversed(ordered):
        _apply_one(spans, repl)


# ── Beschriftung über python-docx ──────────────────────────────────────────

#: Die sechs Kopf-/Fusszeilen-Varianten je Abschnitt.
_HEADER_FOOTER_ATTRS = (
    "header",
    "footer",
    "first_page_header",
    "first_page_footer",
    "even_page_header",
    "even_page_footer",
)


def _labels_from_python_docx(data: bytes) -> Dict[Tuple[str, int], str]:
    """Ermittelt sprechende Namen für alle Absätze, die ``python-docx`` erreicht.

    Das Ergebnis ist reine Beschriftung — es beeinflusst **nicht**, welche
    Absätze verarbeitet werden. Genau darin liegt der Nachweis: Was hier fehlt,
    trägt später eine ``fallback:``-ID und wäre ohne den lxml-Weg unbehandelt
    geblieben.

    Returns
    -------
    dict
        ``(Part-Name, Absatz-Index) -> Label``, z. B.
        ``("word/document.xml", 4) -> "body/t0/r1/c2"``.
    """
    labels: Dict[Tuple[str, int], str] = {}
    try:
        document = Document(io.BytesIO(data))
    except Exception:  # pragma: no cover - defektes Paket, lxml-Weg trägt weiter
        return labels

    # Part-Name -> {Absatz-Element: Index}. Die Liste bleibt referenziert, damit
    # lxml die Element-Proxies nicht freigibt und ihre Identität stabil bleibt.
    index_maps: Dict[str, Tuple[List[etree._Element], Dict[etree._Element, int]]] = {}

    def address(paragraph) -> Optional[Tuple[str, int]]:
        part = paragraph.part
        name = str(part.partname).lstrip("/")
        entry = index_maps.get(name)
        if entry is None:
            element = getattr(part, "element", None)
            if element is None:
                return None
            ordered = list(element.iter(_PARA_TAG))
            entry = (ordered, {node: i for i, node in enumerate(ordered)})
            index_maps[name] = entry
        index = entry[1].get(paragraph._p)
        return None if index is None else (name, index)

    def walk(container, prefix: str) -> None:
        for paragraph in container.paragraphs:
            key = address(paragraph)
            if key is not None:
                # setdefault: Verbundene Tabellenzellen tauchen in row.cells
                # mehrfach auf — das erste Label gewinnt.
                labels.setdefault(key, prefix)
        for t_index, table in enumerate(container.tables):
            for r_index, row in enumerate(table.rows):
                for c_index, cell in enumerate(row.cells):
                    walk(cell, f"{prefix}/t{t_index}/r{r_index}/c{c_index}")

    walk(document, "body")

    for s_index, section in enumerate(document.sections):
        for attr in _HEADER_FOOTER_ATTRS:
            part_of_section = getattr(section, attr)
            # is_linked_to_previous zuerst prüfen: Der Zugriff auf .paragraphs
            # einer verknüpften Kopfzeile legt in python-docx einen neuen
            # Header-Part an und würde das Dokument verändern.
            if part_of_section.is_linked_to_previous:
                continue
            walk(part_of_section, f"{attr}{s_index}")

    return labels


# ── Adapter ────────────────────────────────────────────────────────────────


class DocxAdapter(DocumentAdapter):
    """Formaterhaltender Adapter für Word-Dokumente."""

    content_types = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    extensions = (".docx",)

    # -- Extraktion --------------------------------------------------------

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert ein Segment je Absatz — über **alle** Parts des Pakets."""
        package = _DocxPackage(data)
        return self._build_segments(package, _labels_from_python_docx(data))

    @staticmethod
    def _build_segments(
        package: _DocxPackage,
        labels: Dict[Tuple[str, int], str],
    ) -> List[Segment]:
        """Baut die Segmentliste aus einem geöffneten Paket."""
        segments: List[Segment] = []
        for part_name, paragraphs in package.paragraphs.items():
            for index, paragraph in enumerate(paragraphs):
                text, _spans = _paragraph_spans(paragraph)
                if not text.strip():
                    continue  # leere und rein weisse Absätze tragen keine PII
                label = labels.get((part_name, index))
                segment_id = (
                    f"{label}/p{index}"
                    if label is not None
                    else f"fallback:{part_name}/p{index}"
                )
                segments.append(
                    Segment(
                        id=segment_id,
                        text=text,
                        location=DocxLocation(part_name, index),
                    )
                )
        return segments

    # -- Ersetzung ---------------------------------------------------------

    def apply_replacements(
        self,
        data: bytes,
        replacements: Dict[str, List[Replacement]],
    ) -> bytes:
        """Schreibt die Ersetzungen zurück, ohne die Formatierung anzutasten."""
        package = _DocxPackage(data)
        # Die Segmente werden neu aufgebaut, weil ``replacements`` nach
        # Segment-ID indiziert ist und die Location nicht mitliefert. Da die
        # Extraktion deterministisch ist, entstehen exakt dieselben IDs.
        segments = self._build_segments(package, _labels_from_python_docx(data))
        by_id = {segment.id: segment for segment in segments}

        for segment_id, repls in replacements.items():
            if not repls:
                continue
            segment = by_id.get(segment_id)
            if segment is None:
                # Unbekannte ID: laut Schnittstellenvertrag ignorieren.
                continue
            location: DocxLocation = segment.location
            paragraph = package.paragraphs[location.part_name][location.paragraph_index]
            _apply_to_paragraph(paragraph, segment.text, list(repls))
            package.modified.add(location.part_name)

        return package.to_bytes()

    # -- Metadaten ---------------------------------------------------------

    def strip_metadata(self, data: bytes) -> bytes:
        """Leert Autor, Firma und verwandte Felder in den ``docProps``.

        Geleert werden in ``docProps/core.xml``: ``dc:creator``,
        ``cp:lastModifiedBy``, ``dc:title``, ``dc:subject``, ``cp:keywords``,
        ``dc:description``, ``cp:revision``; in ``docProps/app.xml``:
        ``Company`` und ``Manager``.

        Die Elemente werden **geleert, nicht entfernt** — manche Programme
        (und ``python-docx`` selbst) erwarten sie und legen sie sonst neu an.

        Nicht angetastet: ``docProps/thumbnail.jpeg`` (das Vorschaubild zeigt
        die erste Seite und kann Personendaten enthalten — es zu entfernen
        erfordert auch Anpassungen an ``[Content_Types].xml`` und den
        Beziehungen und gehört deshalb in einen eigenen Schritt) sowie
        ``w:author``-Attribute an Kommentaren und nachverfolgten Änderungen.
        """
        infos, blobs = _read_package(data)
        replaced: Dict[str, bytes] = {}

        for part_name, fields in (
            (_CORE_PROPS_PART, _CORE_FIELDS),
            (_APP_PROPS_PART, _APP_FIELDS),
        ):
            blob = blobs.get(part_name)
            if blob is None:
                continue
            try:
                root = etree.fromstring(blob, _PARSER)
            except etree.XMLSyntaxError:
                continue
            changed = False
            for field_tag in fields:
                for node in root.iter(field_tag):
                    if node.text:
                        node.text = ""
                        changed = True
            if changed:
                replaced[part_name] = _serialize(root)

        return _write_package(infos, blobs, replaced)
