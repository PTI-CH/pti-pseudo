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
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from docx import Document
from lxml import etree

from backend.adapters import ooxml
from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.adapters.ooxml import ParagraphSchema, make_qn, strip_docprops

#: Paket-Ebene: unveraendert aus ``ooxml`` uebernommen. Die modulinternen
#: Namen bleiben, damit die Aufrufstellen unten unberuehrt sind.
_PARSER = ooxml.PARSER
_serialize = ooxml.serialize
_write_package = ooxml.write_package

__all__ = ["DocxAdapter", "DocxLocation"]

# ── Namensräume ────────────────────────────────────────────────────────────
#: Nur der Word-eigene Namensraum steht hier. ``xml``, ``dc``, ``cp`` und
#: ``ep`` gehören zum Paket, nicht zum Format, und kommen aus
#: ``ooxml.PACKAGE_NS`` — dieselben Werte gelten für xlsx und pptx.
_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}

qn = make_qn(_NS)


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

#: Wie Word Absätze, Runs und Text ablegt. PowerPoint benutzt dieselbe
#: Struktur mit ``a:``-Tags — deshalb liegt die Run-Arithmetik in ``ooxml``
#: und nicht hier.
_SCHEMA = ParagraphSchema(
    paragraph=_PARA_TAG,
    run=_RUN_TAG,
    text=_TEXT_TAG,
    separators=_SEPARATOR_TAGS,
    space_attr=_XML_SPACE,
)

#: Der Haupt-Part kommt in der Segmentreihenfolge immer zuerst.
_MAIN_PART = "word/document.xml"


def _read_package(data: bytes):
    """Liest das Paket; die Fehlermeldung nennt DOCX beim Namen."""
    return ooxml.read_package(data, "DOCX")


def _paragraph_spans(paragraph):
    """Absatztext und Run-Landkarte nach Word-Schema."""
    return ooxml.paragraph_spans(paragraph, _SCHEMA)


def _apply_to_paragraph(paragraph, expected_text: str, replacements) -> None:
    """Wendet die Ersetzungen eines Segments nach Word-Schema an."""
    ooxml.apply_to_paragraph(paragraph, expected_text, replacements, _SCHEMA)


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

        Die Umsetzung liegt in ``ooxml.strip_docprops``: die ``docProps``
        gehören zum Paket und nicht zum Format, xlsx und pptx brauchen genau
        dasselbe.
        """
        return strip_docprops(data, "DOCX")
