"""Adapter für PowerPoint-Präsentationen (``.pptx``, OOXML/PresentationML).

PowerPoint legt Text **genauso ab wie Word**, nur im DrawingML-Namensraum:
``a:p`` → ``a:r`` → ``a:t``. Damit gilt hier dasselbe Kernproblem und dieselbe
Lösung wie im DOCX-Adapter — ein Name steht regelmässig über mehrere Runs
verteilt, weil jemand ein Wort nachträglich bearbeitet hat. Die Run-Arithmetik
dafür liegt in ``adapters/ooxml.py`` und wird hier nur mit anderen Tags
aufgerufen; siehe :data:`_SCHEMA`.

Was PowerPoint eigen ist, sind die **Fundorte**. Eine Präsentation trägt Text
an deutlich mehr Stellen als ein Word-Dokument:

* ``ppt/slides/slideN.xml`` — die Folien selbst
* ``ppt/notesSlides/notesSlideN.xml`` — die **Notizen** des Vortragenden.
  Der praktisch wichtigste Fundort nach den Folien: Dort steht regelmässig,
  was auf der Folie bewusst *nicht* steht — Namen von Betroffenen, interne
  Einschätzungen, Fallnummern.
* ``ppt/slideLayouts/``, ``ppt/slideMasters/`` — Layouts und Master. Meist nur
  Platzhaltertexte, aber eben auch Fusszeilen mit Organisations- und
  Bearbeiternamen.
* ``ppt/charts/chartN.xml`` — Diagrammtitel und Achsenbeschriftungen liegen
  als ``a:p`` in ``c:rich``.

Wie beim DOCX-Adapter wird deshalb **generisch über alle XML-Parts** gesucht,
statt eine Liste bekannter Orte zu pflegen: Jeder Part unter ``ppt/`` wird nach
``a:p`` durchsucht. Was eine Liste vergisst, bleibt im Dokument stehen — und
genau das ist das Fehlerbild, das dieses Werkzeug verhindern soll.

Sprechende Namen (``folie3/p0``, ``folie3/notizen/p1``) entstehen aus der
Foliennummer der Präsentation; alles Übrige bekommt ``fallback:<part>``. An der
Segment-ID ist damit ablesbar, woher ein Fund stammt.

Nicht abgedeckt (bewusst)
-------------------------
* **Eingebettete Arbeitsmappen** hinter Diagrammen
  (``ppt/embeddings/*.xlsx``). Das sichtbare Diagramm wird über seinen
  Zwischenspeicher (``c:strCache``) erfasst, die eingebettete Quelldatei
  nicht. Sie formatgerecht zu öffnen hiesse, den XLSX-Adapter rekursiv
  anzuwenden — ein eigener Schritt.
* **Alt-Texte** von Bildern (``p:cNvPr/@descr``) und Kommentare
  (``ppt/comments/``), die ein eigenes, von DrawingML abweichendes Format
  haben.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from lxml import etree

from backend.adapters import ooxml
from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.adapters.ooxml import ParagraphSchema, make_qn, strip_docprops

__all__ = ["PptxAdapter", "PptxLocation"]

# ── Namensräume ────────────────────────────────────────────────────────────
#: ``a`` ist DrawingML (der Text), ``p`` PresentationML (die Folienstruktur).
#: ``r`` und ``pr`` kommen aus ``ooxml.PACKAGE_NS``.
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}

qn = make_qn(_NS)

_PARA_TAG = qn("a:p")
_RUN_TAG = qn("a:r")
_TEXT_TAG = qn("a:t")

#: DrawingML kennt nur den Zeilenumbruch als eigenen Knoten; einen Tabulator
#: schreibt PowerPoint als Zeichen in ``a:t``.
_SEPARATOR_TAGS = {qn("a:br"): "\n"}

#: ``xml:space="preserve"`` wird gesetzt, obwohl DrawingML Leerraum ohnehin
#: bewahrt. Das Attribut ist im ``xml``-Namensraum universell gültig und
#: schadet nicht — und es macht die Zusicherung unabhängig davon, wie streng
#: ein späterer Verarbeiter den Leerraum behandelt.
_SCHEMA = ParagraphSchema(
    paragraph=_PARA_TAG,
    run=_RUN_TAG,
    text=_TEXT_TAG,
    separators=_SEPARATOR_TAGS,
    space_attr=qn("xml:space"),
)

_PRESENTATION_PART = "ppt/presentation.xml"

#: Beziehungstypen, die zu Folien bzw. Notizen führen.
_SLIDE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
_NOTES_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "notesSlide"
)

#: Parts, in denen überhaupt DrawingML-Text stehen kann. Alles ausserhalb von
#: ``ppt/`` (docProps, Beziehungen) ist entweder Metadatum oder Verwaltung.
_TEXT_PART_RE = re.compile(r"^ppt/.*\.xml$")


@dataclass(frozen=True)
class PptxLocation:
    """Adresse eines Absatzes im Paket — für Aufrufer opak.

    Bewusst nur primitive Werte, keine Element-Referenz: Zwischen
    ``extract_segments()`` und ``apply_replacements()`` liegt möglicherweise
    ein anderer HTTP-Request, und das Paket wird dann neu geparst.

    Attributes
    ----------
    part_name
        Name des ZIP-Eintrags, z. B. ``"ppt/slides/slide2.xml"``.
    paragraph_index
        0-basierter Index des ``a:p`` in Dokumentreihenfolge **innerhalb
        dieses Parts**. Stabil, weil der Adapter beim Ersetzen niemals Absätze
        oder Runs hinzufügt oder entfernt.
    """

    part_name: str
    paragraph_index: int


def _rels_part_for(part_name: str) -> str:
    """Der Name des Beziehungs-Parts zu einem Part.

    ``ppt/presentation.xml`` → ``ppt/_rels/presentation.xml.rels``

    Examples
    --------
    >>> _rels_part_for("ppt/slides/slide1.xml")
    'ppt/slides/_rels/slide1.xml.rels'
    """
    ordner, _, datei = part_name.rpartition("/")
    return f"{ordner}/_rels/{datei}.rels" if ordner else f"_rels/{datei}.rels"


def _resolve(base_part: str, target: str) -> str:
    """Löst ein relatives Beziehungsziel gegen den Part auf, der es nennt.

    Ziele sind relativ zum **Ordner** des Parts notiert, und Notizen zeigen
    mit ``../`` aus dem Folienordner heraus.

    Examples
    --------
    >>> _resolve("ppt/presentation.xml", "slides/slide1.xml")
    'ppt/slides/slide1.xml'
    >>> _resolve("ppt/slides/slide1.xml", "../notesSlides/notesSlide1.xml")
    'ppt/notesSlides/notesSlide1.xml'
    """
    if target.startswith("/"):
        return target.lstrip("/")
    ordner = posixpath.dirname(base_part)
    return posixpath.normpath(posixpath.join(ordner, target))


def _relationships(
    blobs: Dict[str, bytes], part_name: str
) -> Dict[str, Tuple[str, str]]:
    """Liest die Beziehungen eines Parts als ``rId -> (Typ, Zielpart)``."""
    root = ooxml.parse_part(blobs, _rels_part_for(part_name))
    if root is None:
        return {}
    beziehungen: Dict[str, Tuple[str, str]] = {}
    for node in root.iter(qn("pr:Relationship")):
        rid = node.get("Id")
        ziel = node.get("Target")
        typ = node.get("Type") or ""
        if not rid or not ziel:
            continue
        if node.get("TargetMode") == "External":
            continue  # zeigt aus dem Paket heraus, kein Part
        beziehungen[rid] = (typ, _resolve(part_name, ziel))
    return beziehungen


def _folien_beschriftung(blobs: Dict[str, bytes]) -> Dict[str, str]:
    """Ordnet Folien- und Notizen-Parts sprechende Namen zu.

    Die Reihenfolge kommt aus ``p:sldIdLst`` in ``ppt/presentation.xml`` —
    also aus der **Präsentationsreihenfolge**, nicht aus den Dateinamen.
    ``slide1.xml`` muss nicht die erste Folie sein: Wer Folien umsortiert,
    ändert die Liste, nicht die Dateinamen.

    Das Ergebnis ist reine Beschriftung; es entscheidet **nicht**, welche
    Parts verarbeitet werden. Genau darin liegt der Nachweis: Was hier fehlt,
    trägt später eine ``fallback:``-ID und wäre ohne den generischen Durchlauf
    unbehandelt geblieben.
    """
    namen: Dict[str, str] = {}
    root = ooxml.parse_part(blobs, _PRESENTATION_PART)
    if root is None:
        return namen

    beziehungen = _relationships(blobs, _PRESENTATION_PART)
    nummer = 0
    for sld_id in root.iter(qn("p:sldId")):
        rid = sld_id.get(qn("r:id"))
        if not rid or rid not in beziehungen:
            continue
        typ, ziel = beziehungen[rid]
        if typ != _SLIDE_REL:
            continue
        nummer += 1
        namen[ziel] = f"folie{nummer}"

        # Die Notizen haengen an der Folie, nicht an der Praesentation.
        for notiz_typ, notiz_ziel in _relationships(blobs, ziel).values():
            if notiz_typ == _NOTES_REL:
                namen[notiz_ziel] = f"folie{nummer}/notizen"
    return namen


class _PptxPackage:
    """Das entpackte Paket mit allen Parts, die DrawingML-Absätze tragen.

    Hält die geparsten lxml-Bäume, damit Extraktion und Ersetzung auf
    **denselben** Objekten arbeiten können.
    """

    def __init__(self, data: bytes) -> None:
        self.infos, self.blobs = ooxml.read_package(data, "PPTX")
        self.roots: Dict[str, etree._Element] = {}
        self.paragraphs: Dict[str, List[etree._Element]] = {}
        self.modified: set[str] = set()

        for name in self._ordered_names():
            root = ooxml.parse_part(self.blobs, name)
            if root is None:
                continue
            absaetze = list(root.iter(_PARA_TAG))
            if not absaetze:
                continue
            self.roots[name] = root
            self.paragraphs[name] = absaetze

    def _ordered_names(self) -> List[str]:
        """Kandidaten-Parts in stabiler Reihenfolge: Folien, dann der Rest.

        Die Reihenfolge bestimmt die Segmentreihenfolge und muss deshalb
        deterministisch sein — die Determinismus-Pflicht aus
        ``adapters/base.py``. Innerhalb der beiden Gruppen wird alphabetisch
        sortiert, weil die ZIP-Reihenfolge nichts zusichert.
        """
        beschriftet = _folien_beschriftung(self.blobs)
        kandidaten = [n for n in self.blobs if _TEXT_PART_RE.match(n)]
        # Folien und ihre Notizen zuerst, in Praesentationsreihenfolge.
        reihenfolge = list(beschriftet)
        rest = sorted(n for n in kandidaten if n not in beschriftet)
        return [n for n in reihenfolge if n in self.blobs] + rest

    def to_bytes(self) -> bytes:
        """Serialisiert die geänderten Parts und packt das Paket neu."""
        replaced = {name: ooxml.serialize(self.roots[name]) for name in self.modified}
        return ooxml.write_package(self.infos, self.blobs, replaced)


class PptxAdapter(DocumentAdapter):
    """Formaterhaltender Adapter für PowerPoint-Präsentationen."""

    content_types = (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    extensions = (".pptx",)

    # -- Extraktion --------------------------------------------------------

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert ein Segment je Absatz — über **alle** Parts des Pakets."""
        package = _PptxPackage(data)
        return self._build_segments(package)

    @staticmethod
    def _build_segments(package: _PptxPackage) -> List[Segment]:
        """Baut die Segmentliste aus einem geöffneten Paket."""
        labels = _folien_beschriftung(package.blobs)
        segments: List[Segment] = []
        for part_name, absaetze in package.paragraphs.items():
            label: Optional[str] = labels.get(part_name)
            for index, absatz in enumerate(absaetze):
                text, _spans = ooxml.paragraph_spans(absatz, _SCHEMA)
                if not text.strip():
                    continue  # leere und rein weisse Absätze tragen keine PII
                segment_id = (
                    f"{label}/p{index}"
                    if label is not None
                    else f"fallback:{part_name}/p{index}"
                )
                segments.append(
                    Segment(
                        id=segment_id,
                        text=text,
                        location=PptxLocation(part_name, index),
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
        package = _PptxPackage(data)
        # Die Segmente werden neu aufgebaut, weil ``replacements`` nach
        # Segment-ID indiziert ist und die Location nicht mitliefert. Da die
        # Extraktion deterministisch ist, entstehen exakt dieselben IDs.
        by_id = {segment.id: segment for segment in self._build_segments(package)}

        for segment_id, repls in replacements.items():
            if not repls:
                continue
            segment = by_id.get(segment_id)
            if segment is None:
                # Unbekannte ID: laut Schnittstellenvertrag ignorieren.
                continue
            location: PptxLocation = segment.location
            absatz = package.paragraphs[location.part_name][location.paragraph_index]
            ooxml.apply_to_paragraph(absatz, segment.text, list(repls), _SCHEMA)
            package.modified.add(location.part_name)

        return package.to_bytes()

    # -- Metadaten ---------------------------------------------------------

    def strip_metadata(self, data: bytes) -> bytes:
        """Leert Autor, Firma und verwandte Felder in den ``docProps``.

        Identisch zu Word und Excel — die ``docProps`` gehören zum Paket und
        nicht zum Format. Details in ``ooxml.strip_docprops``.
        """
        return strip_docprops(data, "PPTX")
