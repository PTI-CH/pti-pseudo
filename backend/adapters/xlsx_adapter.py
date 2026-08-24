"""Adapter für Excel-Arbeitsmappen (``.xlsx``, OOXML/SpreadsheetML).

Anders als Word und PowerPoint legt Excel Text **nicht** als Absatz aus Runs
ab, sondern in Zellen — und dazu mit einer Eigenheit, die jeder naive
Ersetzungsversuch übersieht.

Die Falle: die Tabelle der gemeinsamen Zeichenketten
----------------------------------------------------
Zellinhalte stehen in aller Regel nicht in der Zelle, sondern in
``xl/sharedStrings.xml``; die Zelle trägt nur einen **Index** darauf
(``<c r="B4" t="s"><v>17</v></c>``). Zwei Zellen mit demselben Text teilen
sich denselben Eintrag — das ist der Sinn der Tabelle.

Wer nun den Eintrag Nr. 17 überschreibt, um Zelle B4 zu pseudonymisieren,
ändert damit **jede andere Zelle mit demselben Text mit**. Steht „Meier" in
einer Kopfzeile als Spaltenüberschrift und weiter unten als Nachname, würde
die Überschrift stillschweigend zu ``[[PERSON_1]]``.

Dieser Adapter fasst die Tabelle deshalb **nie** an. Eine geänderte Zelle wird
auf eine *eingebettete* Zeichenkette umgestellt
(``<c t="inlineStr"><is><t>…</t></is></c>``) — ein im Format vorgesehener,
gleichwertiger Weg. Damit ist die Änderung strukturell auf genau diese eine
Zelle beschränkt, statt sich auf eine Absprache zu verlassen. Der verwaiste
Eintrag in der Tabelle bleibt stehen; er ist unschädlich, und ihn zu entfernen
hiesse, alle Indizes dahinter zu verschieben.

Ein Segment je Zeile, nicht je Zelle
------------------------------------
Wie beim CSV-Adapter, und aus denselben Gründen: Eine Zeile ist ein Datensatz.
Der Kontext, den die Erkennung braucht, steht *innerhalb* der Zeile. Eine
einzelne Zelle („Meier") gäbe dem Sprachmodell nichts zu arbeiten, und die
Nachbarzeile ist ein anderer Fall und liefert nur Rauschen. Die Zellen einer
Zeile werden dafür mit Tabulator verbunden — dasselbe Trennzeichen, das auch
der DOCX-Adapter für ``w:tab`` einsetzt, damit die Erkennung nicht
``"MeierAndreas"`` als ein Wort sieht.

Die Segment-ID nennt Blatt und Excel-Zeilennummer (``Personen/z12``) — die
Adresse, die Benutzer in der Befundliste erwarten.

Nicht abgedeckt (bewusst)
-------------------------
* **Formeln.** Trägt eine Zelle eine Formel, steht im Dokument zusätzlich ihr
  zwischengespeichertes Ergebnis. Beides zugleich stimmig zu halten ist nicht
  möglich: Lässt man die Formel stehen, holt die nächste Neuberechnung den
  Klarnamen zurück; entfernt man sie, verliert die Mappe ihre Rechnung. Dieser
  Adapter **entfernt die Formel** und schreibt den Ersatztext als festen Wert
  — im Zweifel für den Schutz. Betroffen sind nur Zellen, in denen tatsächlich
  ein Fund ersetzt wird; alle anderen Formeln bleiben unberührt.
* **Kommentare** (``xl/comments*.xml``) und **Notizen**. Sie haben ein
  eigenes, von den Zellen abweichendes Format.
* **Diagramme** und eingebettete Objekte.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from lxml import etree

from backend.adapters import ooxml
from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.adapters.ooxml import make_qn, strip_docprops, validate_replacements

__all__ = ["XlsxAdapter", "XlsxLocation"]

# ── Namensräume ────────────────────────────────────────────────────────────
_NS = {
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}

qn = make_qn(_NS)

_ROW_TAG = qn("x:row")
_CELL_TAG = qn("x:c")
_VALUE_TAG = qn("x:v")
_FORMULA_TAG = qn("x:f")
_INLINE_TAG = qn("x:is")
_TEXT_TAG = qn("x:t")
_SI_TAG = qn("x:si")

_WORKBOOK_PART = "xl/workbook.xml"
_SHARED_STRINGS_PART = "xl/sharedStrings.xml"

_SHEET_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)

#: Trennzeichen zwischen den Zellen einer Zeile im Segmenttext.
_CELL_SEPARATOR = "\t"

_SHEET_PART_RE = re.compile(r"^xl/worksheets/[^/]+\.xml$")


@dataclass(frozen=True)
class XlsxLocation:
    """Adresse einer Zeile im Paket — für Aufrufer opak.

    Attributes
    ----------
    part_name
        Name des ZIP-Eintrags, z. B. ``"xl/worksheets/sheet1.xml"``.
    row_index
        0-basierte Position des ``x:row`` **innerhalb dieses Blatts**, nicht
        die Excel-Zeilennummer. Positionen sind stabil, weil der Adapter beim
        Ersetzen niemals Zeilen oder Zellen hinzufügt oder entfernt; die
        Excel-Nummer (``r``) darf dagegen Lücken haben.
    """

    part_name: str
    row_index: int


@dataclass
class _Zelle:
    """Eine Zelle mit Text und ihrer Lage im Zeilentext."""

    node: etree._Element
    text: str
    start: int
    end: int


def _rels_part_for(part_name: str) -> str:
    """Der Name des Beziehungs-Parts zu einem Part."""
    ordner, _, datei = part_name.rpartition("/")
    return f"{ordner}/_rels/{datei}.rels" if ordner else f"_rels/{datei}.rels"


def _resolve(base_part: str, target: str) -> str:
    """Löst ein relatives Beziehungsziel gegen den Part auf, der es nennt."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _shared_strings(blobs: Dict[str, bytes]) -> List[str]:
    """Liest ``xl/sharedStrings.xml`` als Liste in Indexreihenfolge.

    Ein Eintrag kann formatierten Text tragen und dann aus mehreren
    ``x:r``-Runs bestehen. Der Zelltext ist ihre Verkettung — deshalb werden
    alle ``x:t`` unterhalb des Eintrags eingesammelt, nicht nur das erste.
    """
    root = ooxml.parse_part(blobs, _SHARED_STRINGS_PART)
    if root is None:
        return []
    eintraege: List[str] = []
    for si in root.iter(_SI_TAG):
        eintraege.append("".join(t.text or "" for t in si.iter(_TEXT_TAG)))
    return eintraege


def _blatt_namen(blobs: Dict[str, bytes]) -> Dict[str, str]:
    """Ordnet Blatt-Parts ihren sichtbaren Namen zu, in Mappenreihenfolge.

    Die Reihenfolge kommt aus ``x:sheets`` in ``xl/workbook.xml`` — also aus
    der Reihenfolge der Registerkarten, nicht aus den Dateinamen.
    ``sheet1.xml`` muss nicht das erste Blatt sein.

    Wie beim DOCX- und PPTX-Adapter ist das reine Beschriftung: Sie
    entscheidet **nicht**, welche Parts verarbeitet werden. Ein Blatt, das
    hier fehlt, trägt später eine ``fallback:``-ID.
    """
    namen: Dict[str, str] = {}
    root = ooxml.parse_part(blobs, _WORKBOOK_PART)
    if root is None:
        return namen

    rels_root = ooxml.parse_part(blobs, _rels_part_for(_WORKBOOK_PART))
    ziele: Dict[str, str] = {}
    if rels_root is not None:
        for node in rels_root.iter(qn("pr:Relationship")):
            rid, ziel, typ = node.get("Id"), node.get("Target"), node.get("Type") or ""
            if not (rid and ziel) or typ != _SHEET_REL:
                continue
            if node.get("TargetMode") == "External":
                continue  # zeigt aus dem Paket heraus, kein Part
            ziele[rid] = _resolve(_WORKBOOK_PART, ziel)

    for sheet in root.iter(qn("x:sheet")):
        rid = sheet.get(qn("r:id"))
        name = sheet.get("name")
        if rid and name and rid in ziele:
            namen[ziele[rid]] = name
    return namen


def _zelltext(zelle: etree._Element, gemeinsame: List[str]) -> str:
    """Der sichtbare Text einer Zelle, oder ``""`` wenn sie keinen trägt.

    Zahlen, Wahrheitswerte und Fehler liefern bewusst ``""``: Sie sind keine
    Zeichenketten, und die Erkennung hätte an ihnen nichts zu suchen. Eine
    Zahl kann Personendaten tragen (eine AHV-Nummer etwa), steht dann aber in
    Excel praktisch immer als Text — sonst verlöre sie ihre führenden Nullen
    und Punkte.
    """
    typ = zelle.get("t")
    if typ == "inlineStr":
        inline = zelle.find(_INLINE_TAG)
        if inline is None:
            return ""
        return "".join(t.text or "" for t in inline.iter(_TEXT_TAG))
    wert = zelle.find(_VALUE_TAG)
    if wert is None:
        return ""
    roh = wert.text or ""
    if typ == "s":
        try:
            return gemeinsame[int(roh)]
        except (ValueError, IndexError):
            # Kaputter Index: lieber kein Text als ein falscher. Die Zelle
            # taucht dann nicht in der Erkennung auf.
            return ""
    if typ == "str":
        return roh  # zwischengespeichertes Ergebnis einer Formel
    return ""  # Zahl, Wahrheitswert, Fehler


def _setze_zelltext(zelle: etree._Element, text: str) -> None:
    """Schreibt Text als eingebettete Zeichenkette in die Zelle.

    Die Tabelle der gemeinsamen Zeichenketten bleibt unberührt — siehe den
    Modulkopf. Eine vorhandene Formel wird entfernt: Bliebe sie stehen, holte
    die nächste Neuberechnung den Klarnamen zurück.
    """
    for kind in list(zelle):
        if kind.tag in (_VALUE_TAG, _INLINE_TAG, _FORMULA_TAG):
            zelle.remove(kind)
    zelle.set("t", "inlineStr")
    inline = etree.SubElement(zelle, _INLINE_TAG)
    knoten = etree.SubElement(inline, _TEXT_TAG)
    knoten.text = text
    if text[:1].isspace() or text[-1:].isspace():
        knoten.set(qn("xml:space"), "preserve")


def _zeilen_landkarte(
    zeile: etree._Element, gemeinsame: List[str]
) -> Tuple[str, List[_Zelle]]:
    """Verbindet die Zellen einer Zeile und liefert die Landkarte dazu.

    Returns
    -------
    (text, zellen)
        ``text`` ist der Segmenttext (Zellen mit Tabulator verbunden),
        ``zellen`` die Zuordnung von Offset-Bereichen zu Zellknoten. Nur
        Zellen **mit** Text stehen darin; leere Zellen erzeugen keinen
        Tabulator, sonst bestünde eine Zeile mit einem Namen in Spalte Z aus
        25 Tabulatoren und die Erkennung sähe nur Rauschen.
    """
    zellen: List[_Zelle] = []
    stuecke: List[str] = []
    offset = 0
    for zelle in zeile:
        if zelle.tag != _CELL_TAG:
            continue
        text = _zelltext(zelle, gemeinsame)
        if not text:
            continue
        if stuecke:
            stuecke.append(_CELL_SEPARATOR)
            offset += len(_CELL_SEPARATOR)
        zellen.append(
            _Zelle(node=zelle, text=text, start=offset, end=offset + len(text))
        )
        stuecke.append(text)
        offset += len(text)
    return "".join(stuecke), zellen


def _wende_an(zellen: List[_Zelle], repl: Replacement) -> None:
    """Wendet **eine** Ersetzung auf die Zellen-Landkarte an.

    Der Aufrufer garantiert Anwendung von rechts nach links; deshalb dürfen
    hier die ursprünglichen Offsets gegen den aktuellen Zelltext gerechnet
    werden.

    Läuft ein Fund über eine Zellgrenze — das ist möglich, weil die Erkennung
    den verbundenen Zeilentext sieht —, geht der Ersatztext **vollständig in
    die erste berührte Zelle**; die weiteren verlieren nur ihren
    überlappenden Teil. Dieselbe Regel wie bei den Runs im DOCX-Adapter.
    """
    beruehrt = [z for z in zellen if z.start < repl.end and z.end > repl.start]
    if not beruehrt:
        # Reine Einfuegung an einer Zellgrenze: die naechstgelegene Zelle
        # nimmt sie auf, bevorzugt die davor liegende.
        davor = [z for z in zellen if z.end <= repl.start]
        danach = [z for z in zellen if z.start >= repl.start]
        ziel = davor[-1] if davor else (danach[0] if danach else None)
        if ziel is None:
            raise ValueError(
                f"Für die Einfügung an Position {repl.start} gibt es keine "
                "Zelle mit Text in dieser Zeile."
            )
        stelle = len(ziel.text) if davor else 0
        neu = ziel.text[:stelle] + repl.text + ziel.text[stelle:]
        _setze_zelltext(ziel.node, neu)
        ziel.text = neu
        return

    ziel = beruehrt[0]
    for zelle in beruehrt:
        lokal_start = max(0, repl.start - zelle.start)
        lokal_ende = min(len(zelle.text), repl.end - zelle.start)
        if zelle is ziel:
            neu = zelle.text[:lokal_start] + repl.text + zelle.text[lokal_ende:]
        else:
            neu = zelle.text[:lokal_start] + zelle.text[lokal_ende:]
        _setze_zelltext(zelle.node, neu)
        zelle.text = neu


class _XlsxPackage:
    """Das entpackte Paket mit allen Blättern und ihren Zeilen."""

    def __init__(self, data: bytes) -> None:
        self.infos, self.blobs = ooxml.read_package(data, "XLSX")
        self.gemeinsame = _shared_strings(self.blobs)
        self.roots: Dict[str, etree._Element] = {}
        self.rows: Dict[str, List[etree._Element]] = {}
        self.modified: set[str] = set()

        for name in self._ordered_names():
            root = ooxml.parse_part(self.blobs, name)
            if root is None:
                continue
            zeilen = list(root.iter(_ROW_TAG))
            if not zeilen:
                continue
            self.roots[name] = root
            self.rows[name] = zeilen

    def _ordered_names(self) -> List[str]:
        """Blätter in Mappenreihenfolge, danach alphabetisch alles Übrige.

        Die Reihenfolge bestimmt die Segmentreihenfolge und muss
        deterministisch sein (Determinismus-Pflicht aus ``adapters/base.py``).
        """
        benannt = _blatt_namen(self.blobs)
        rest = sorted(
            n for n in self.blobs if _SHEET_PART_RE.match(n) and n not in benannt
        )
        return [n for n in benannt if n in self.blobs] + rest

    def to_bytes(self) -> bytes:
        """Serialisiert die geänderten Blätter und packt das Paket neu."""
        replaced = {name: ooxml.serialize(self.roots[name]) for name in self.modified}
        return ooxml.write_package(self.infos, self.blobs, replaced)


class XlsxAdapter(DocumentAdapter):
    """Formaterhaltender Adapter für Excel-Arbeitsmappen."""

    content_types = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    extensions = (".xlsx",)

    # -- Extraktion --------------------------------------------------------

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert ein Segment je Zeile mit Text — über alle Blätter."""
        return self._build_segments(_XlsxPackage(data))

    @staticmethod
    def _build_segments(package: _XlsxPackage) -> List[Segment]:
        """Baut die Segmentliste aus einem geöffneten Paket."""
        namen = _blatt_namen(package.blobs)
        segments: List[Segment] = []
        for part_name, zeilen in package.rows.items():
            blatt: Optional[str] = namen.get(part_name)
            for index, zeile in enumerate(zeilen):
                text, _zellen = _zeilen_landkarte(zeile, package.gemeinsame)
                if not text.strip():
                    continue
                # Die Excel-Zeilennummer ist die Adresse, die Benutzer
                # erwarten — nicht die Position im Part.
                nummer = zeile.get("r") or str(index + 1)
                segment_id = (
                    f"{blatt}/z{nummer}"
                    if blatt is not None
                    else f"fallback:{part_name}/z{nummer}"
                )
                segments.append(
                    Segment(
                        id=segment_id,
                        text=text,
                        location=XlsxLocation(part_name, index),
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
        package = _XlsxPackage(data)
        by_id = {segment.id: segment for segment in self._build_segments(package)}

        for segment_id, repls in replacements.items():
            if not repls:
                continue
            segment = by_id.get(segment_id)
            if segment is None:
                continue  # Unbekannte ID: laut Vertrag ignorieren.
            location: XlsxLocation = segment.location
            zeile = package.rows[location.part_name][location.row_index]
            text, zellen = _zeilen_landkarte(zeile, package.gemeinsame)
            if text != segment.text:
                raise ValueError(
                    "Die Zeile passt nicht mehr zum Segment. Wurden "
                    "extract_segments() und apply_replacements() mit denselben "
                    "Bytes aufgerufen?"
                )
            # Von rechts nach links — sonst verschiebt jede Ersetzung die
            # Offsets der noch ausstehenden um ihre Längendifferenz.
            for repl in reversed(validate_replacements(list(repls), len(text))):
                _wende_an(zellen, repl)
            package.modified.add(location.part_name)

        return package.to_bytes()

    # -- Metadaten ---------------------------------------------------------

    def strip_metadata(self, data: bytes) -> bytes:
        """Leert Autor, Firma und verwandte Felder in den ``docProps``.

        Identisch zu Word und PowerPoint — die ``docProps`` gehören zum Paket
        und nicht zum Format. Details in ``ooxml.strip_docprops``.
        """
        return strip_docprops(data, "XLSX")
