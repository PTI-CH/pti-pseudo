"""Tests des DOCX-Adapters.

Alle Testdokumente entstehen **programmatisch** in den Fixtures unten — es
liegen keine Binärdateien im Repository und, gemäss Hausregel, keine echten
Personendaten. Die verwendeten Namen sind frei erfunden; E-Mail-Adressen nutzen
die reservierte Domain ``example.invalid``.

Das „schmutzige" Testdokument bildet gezielt die Fälle ab, an denen naive
Word-Verarbeitung scheitert:

* ein Absatz, dessen Name über **drei Runs** verteilt ist,
* ein Absatz mit Runs in ``w:hyperlink`` und ``w:ins`` (nachverfolgte Änderung),
* eine Tabelle mit Text in den Zellen,
* Kopf- und Fusszeile,
* eine **Textbox** (``w:txbxContent``) — verschachtelt in einem Run,
* eine **Fussnote** (``word/footnotes.xml``) — ein eigener Part.

Die letzten beiden erreicht ``python-docx`` nicht. Weil ``python-docx`` 1.2.0
auch keine API zum *Erzeugen* von Fussnoten hat, wird der Part nach dem
Speichern mit ``zipfile`` und ``lxml`` von Hand ins Paket gehängt: Part-Datei,
``Override`` in ``[Content_Types].xml``, Beziehung in
``word/_rels/document.xml.rels`` und ein ``w:footnoteReference`` im Body. Genau
so sieht ein von Word erzeugtes Dokument aus.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest
from docx import Document
from docx.oxml import parse_xml
from lxml import etree

# Das Repository-Wurzelverzeichnis muss importierbar sein, auch wenn pytest aus
# einem anderen Arbeitsverzeichnis gestartet wird.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters import get_adapter  # noqa: E402
from backend.adapters.base import Replacement  # noqa: E402
from backend.adapters.docx_adapter import DocxAdapter  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

# ── Erwartete Inhalte (frei erfunden) ──────────────────────────────────────

SATZ = "Andreas Meier wohnt in Bern und arbeitet mit Beat Vogel zusammen."
#: Bewusst in **einem einzigen** Run — nur so landen zwei Ersetzungen im selben
#: w:t-Knoten, und nur dann ist die Reihenfolge rechts-nach-links überhaupt
#: beobachtbar. Über getrennte Runs hinweg wäre jede Reihenfolge richtig.
EIN_RUN_SATZ = "Andreas Meier und Beat Vogel gehören zur selben Abteilung."
#: Runs, die nicht direkte Kinder des Absatzes sind: einer steckt in einem
#: ``w:hyperlink``, einer in einer nachverfolgten Einfügung (``w:ins``). Beides
#: kommt in echten Word-Dokumenten dauernd vor.
VERSCHACHTELT = "Quelle: Andreas Meier (bestätigt durch Beat Vogel)"
KOPFZEILE = "Kanzlei Muster, zuständig: Andreas Meier"
FUSSZEILE = "Seite 1 von 1, erstellt von Beat Vogel"
ZELLE_A = "Andreas Meier"
ZELLE_B = "Bern"
ZELLE_C = "Beat Vogel"
ZELLE_D = "Thun"
TEXTBOX_TEXT = "Randnotiz: Andreas Meier war nicht anwesend."
FUSSNOTE_TEXT = " Aussage von Andreas Meier, protokolliert am 3. Mai."
AUTOR = "Testautor Muster"
FIRMA = "Muster Treuhand AG"


# ── Roh-XML für die Teile, die python-docx nicht erzeugen kann ─────────────

_TEXTBOX_RUN_XML = f"""
<w:r xmlns:w="{W}"
     xmlns:v="urn:schemas-microsoft-com:vml"
     xmlns:w10="urn:schemas-microsoft-com:office:word">
  <w:pict>
    <v:shape id="Textfeld1" type="#_x0000_t202"
             style="width:220pt;height:40pt">
      <v:textbox>
        <w:txbxContent>
          <w:p><w:r><w:t>{TEXTBOX_TEXT}</w:t></w:r></w:p>
        </w:txbxContent>
      </v:textbox>
    </v:shape>
  </w:pict>
</w:r>
"""

_HYPERLINK_XML = f"""
<w:hyperlink xmlns:w="{W}" w:anchor="oben">
  <w:r><w:t>Andreas Meier</w:t></w:r>
</w:hyperlink>
"""

_TRACKED_INSERT_XML = f"""
<w:ins xmlns:w="{W}" w:id="900" w:author="Testautor Muster"
       w:date="2026-01-01T00:00:00Z">
  <w:r><w:t xml:space="preserve"> (bestätigt durch Beat Vogel)</w:t></w:r>
</w:ins>
"""

_FOOTNOTE_REF_RUN_XML = f"""
<w:r xmlns:w="{W}">
  <w:rPr><w:vertAlign w:val="superscript"/></w:rPr>
  <w:footnoteReference w:id="1"/>
</w:r>
"""

_FOOTNOTES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="{W}">
  <w:footnote w:type="separator" w:id="-1">
    <w:p><w:r><w:separator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0">
    <w:p><w:r><w:continuationSeparator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:id="1">
    <w:p>
      <w:r><w:footnoteRef/></w:r>
      <w:r><w:t xml:space="preserve">{FUSSNOTE_TEXT}</w:t></w:r>
    </w:p>
  </w:footnote>
</w:footnotes>
"""

_FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)
_FOOTNOTES_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)


def _inject_footnotes_part(data: bytes) -> bytes:
    """Hängt ``word/footnotes.xml`` samt Content-Type und Beziehung ins Paket.

    python-docx 1.2.0 kennt keine Fussnoten-API — deshalb dieser Umweg über
    ``zipfile``/``lxml``. Er erzeugt exakt die Struktur, die Word schreibt, und
    ist damit ein ehrlicher Testfall für den lxml-Fallback des Adapters.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = {info.filename: zf.read(info.filename) for info in zf.infolist()}
        order = [info.filename for info in zf.infolist()]

    # 1. Content-Type-Override eintragen.
    types = etree.fromstring(entries["[Content_Types].xml"])
    override = etree.SubElement(types, f"{{{CT}}}Override")
    override.set("PartName", "/word/footnotes.xml")
    override.set("ContentType", _FOOTNOTES_CONTENT_TYPE)
    entries["[Content_Types].xml"] = etree.tostring(
        types, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 2. Beziehung vom Hauptdokument auf den neuen Part.
    rels = etree.fromstring(entries["word/_rels/document.xml.rels"])
    relationship = etree.SubElement(rels, f"{{{PR}}}Relationship")
    relationship.set("Id", "rIdFootnotesPTI")
    relationship.set("Type", _FOOTNOTES_REL_TYPE)
    relationship.set("Target", "footnotes.xml")
    entries["word/_rels/document.xml.rels"] = etree.tostring(
        rels, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 3. Der Part selbst.
    entries["word/footnotes.xml"] = _FOOTNOTES_XML.encode("utf-8")
    order.append("word/footnotes.xml")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in order:
            zf.writestr(name, entries[name])
    return buffer.getvalue()


def _set_app_property(data: bytes, name: str, value: str) -> bytes:
    """Setzt ein Feld in ``docProps/app.xml`` (z. B. ``Company``).

    python-docx exponiert ``app.xml`` nicht — für den Metadaten-Test muss die
    Firma deshalb direkt ins XML geschrieben werden.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = {info.filename: zf.read(info.filename) for info in zf.infolist()}
        order = [info.filename for info in zf.infolist()]

    root = etree.fromstring(entries["docProps/app.xml"])
    node = root.find(f"{{{EP}}}{name}")
    if node is None:
        node = etree.SubElement(root, f"{{{EP}}}{name}")
    node.text = value
    entries["docProps/app.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry_name in order:
            zf.writestr(entry_name, entries[entry_name])
    return buffer.getvalue()


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dirty_docx() -> bytes:
    """Erzeugt das absichtlich „schmutzige" Testdokument als Bytes."""
    document = Document()

    # (a) Der Kernfall: ein Name, den Word über drei Runs zerlegt hat.
    paragraph = document.add_paragraph()
    paragraph.add_run("Andreas ")
    paragraph.add_run("Mei")
    paragraph.add_run("er")
    paragraph.add_run(" wohnt in Bern und arbeitet mit Beat Vogel zusammen.")
    # Fussnoten-Verweis im Fliesstext, wie Word ihn setzt.
    paragraph._p.append(parse_xml(_FOOTNOTE_REF_RUN_XML))

    # (b) Ein Absatz mit genau einem Run und zwei Namen darin.
    document.add_paragraph().add_run(EIN_RUN_SATZ)

    # (c) Runs, die in Zwischenelementen stecken: Hyperlink und nachverfolgte
    #     Einfügung. Wer nur die direkten w:r-Kinder eines Absatzes liest,
    #     sieht hier nur "Quelle: ".
    nested = document.add_paragraph()
    nested.add_run("Quelle: ")
    nested._p.append(parse_xml(_HYPERLINK_XML))
    nested._p.append(parse_xml(_TRACKED_INSERT_XML))

    # (d) Ein Absatz, der eine Textbox trägt. Der sichtbare Absatztext ist nur
    #     "Anhang:" — der Textbox-Inhalt ist ein eigener, verschachtelter w:p.
    box_paragraph = document.add_paragraph()
    box_paragraph.add_run("Anhang:")
    box_paragraph._p.append(parse_xml(_TEXTBOX_RUN_XML))

    # (e) Tabelle mit Text in allen vier Zellen.
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = ZELLE_A
    table.cell(0, 1).text = ZELLE_B
    table.cell(1, 0).text = ZELLE_C
    table.cell(1, 1).text = ZELLE_D

    # (f) Kopf- und Fusszeile.
    section = document.sections[0]
    section.header.paragraphs[0].text = KOPFZEILE
    section.footer.paragraphs[0].text = FUSSZEILE

    # (g) Metadaten, die strip_metadata später entfernen muss.
    document.core_properties.author = AUTOR
    document.core_properties.last_modified_by = AUTOR
    document.core_properties.title = "Interner Entwurf"

    buffer = io.BytesIO()
    document.save(buffer)
    data = _inject_footnotes_part(buffer.getvalue())
    return _set_app_property(data, "Company", FIRMA)


@pytest.fixture(scope="module")
def adapter() -> DocxAdapter:
    """Der zu testende Adapter (zustandslos, daher modulweit teilbar)."""
    return DocxAdapter()


# ── Hilfsfunktionen für die Prüfungen ──────────────────────────────────────


def _all_text(data: bytes) -> List[str]:
    """Liest alle sichtbaren Absatztexte aus allen Parts — unabhängig vom Adapter."""
    texts: List[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in sorted(n for n in zf.namelist() if n.endswith(".xml")):
            try:
                root = etree.fromstring(zf.read(name))
            except etree.XMLSyntaxError:
                continue
            if not str(root.tag).startswith(f"{{{W}}}"):
                continue
            for paragraph in root.iter(f"{{{W}}}p"):
                value = "".join(
                    node.text or "" for node in paragraph.iter(f"{{{W}}}t")
                )
                if value.strip():
                    texts.append(value)
    return texts


def _segment_with(segments, needle: str):
    """Liefert das eine Segment, das ``needle`` enthält."""
    hits = [s for s in segments if needle in s.text]
    assert len(hits) == 1, f"{needle!r} kam {len(hits)}-mal vor, erwartet genau einmal"
    return hits[0]


def _replace_all(text: str, needle: str, value: str) -> Replacement:
    """Baut eine Ersetzung für das erste Vorkommen von ``needle``."""
    start = text.index(needle)
    return Replacement(start=start, end=start + len(needle), text=value)


# ── 1. Roundtrip ohne Änderung ─────────────────────────────────────────────


def test_roundtrip_ohne_aenderung_erhaelt_dokument(adapter, dirty_docx):
    """Extrahieren und mit leerer Ersetzungsliste zurückschreiben ändert nichts."""
    vorher = adapter.extract_segments(dirty_docx)
    ausgabe = adapter.apply_replacements(dirty_docx, {})

    # Das Ergebnis ist ein gültiges DOCX und mit python-docx wieder öffenbar.
    wieder = Document(io.BytesIO(ausgabe))
    assert wieder.paragraphs, "Das zurückgeschriebene Dokument hat keine Absätze"

    # Der gesamte Text ist identisch — in allen Parts.
    assert _all_text(ausgabe) == _all_text(dirty_docx)

    # Und die Extraktion liefert dieselben Segmente.
    nachher = adapter.extract_segments(ausgabe)
    assert [(s.id, s.text) for s in nachher] == [(s.id, s.text) for s in vorher]


def test_extraktion_ist_deterministisch_und_ueberlappungsfrei(adapter, dirty_docx):
    """IDs sind eindeutig und kein Absatz wird zweimal erfasst."""
    segments = adapter.extract_segments(dirty_docx)

    ids = [s.id for s in segments]
    assert len(ids) == len(set(ids)), f"Doppelte Segment-IDs: {ids}"

    locations = [(s.location.part_name, s.location.paragraph_index) for s in segments]
    assert len(locations) == len(set(locations)), "Ein Absatz wurde doppelt erfasst"

    # Zweiter Durchlauf muss identisch sein — sonst zeigen Offsets aus einem
    # früheren Request beim Anwenden ins Leere.
    again = adapter.extract_segments(dirty_docx)
    assert [(s.id, s.text, s.location) for s in again] == [
        (s.id, s.text, s.location) for s in segments
    ]


# ── 2. Run-übergreifende Ersetzung ─────────────────────────────────────────


def test_ersetzung_ueber_drei_runs(adapter, dirty_docx):
    """„Andreas Meier" liegt in drei Runs und wird trotzdem als Einheit ersetzt."""
    segments = adapter.extract_segments(dirty_docx)
    segment = _segment_with(segments, "wohnt in Bern")

    # Vorbedingung des Tests: Der Name ist im XML tatsächlich zerstückelt.
    document = Document(io.BytesIO(dirty_docx))
    runs = [r.text for r in document.paragraphs[0].runs if r.text]
    assert runs[:3] == ["Andreas ", "Mei", "er"], f"Unerwartete Run-Aufteilung: {runs}"

    # Das Segment enthält den Namen trotzdem am Stück.
    assert segment.text == SATZ

    ausgabe = adapter.apply_replacements(
        dirty_docx,
        {segment.id: [_replace_all(segment.text, "Andreas Meier", "[[PERSON_1]]")]},
    )

    neu = adapter.extract_segments(ausgabe)
    treffer = _segment_with(neu, "wohnt in Bern")
    assert (
        treffer.text
        == "[[PERSON_1]] wohnt in Bern und arbeitet mit Beat Vogel zusammen."
    )
    assert "Andreas" not in treffer.text
    assert "Meier" not in treffer.text

    # Der Rest des Satzes ist unversehrt, und die leer gewordenen Runs stehen
    # noch da (Löschen würde Word-interne Verweise brechen).
    document_neu = Document(io.BytesIO(ausgabe))
    absatz = document_neu.paragraphs[0]
    assert len(absatz.runs) == len(document.paragraphs[0].runs)
    assert absatz.runs[0].text == "[[PERSON_1]]"
    assert absatz.runs[1].text == ""
    assert absatz.runs[2].text == ""
    assert absatz.runs[3].text == " wohnt in Bern und arbeitet mit Beat Vogel zusammen."


def test_runs_in_hyperlink_und_nachverfolgter_aenderung(adapter, dirty_docx):
    """Runs stecken oft in ``w:hyperlink`` oder ``w:ins`` — nicht direkt im Absatz.

    Wer nur ``w:p/w:r`` liest, sieht von diesem Absatz nur „Quelle: " und
    übersieht beide Namen. Der Adapter steigt deshalb rekursiv ab.
    """
    segments = adapter.extract_segments(dirty_docx)
    segment = _segment_with(segments, "Quelle:")
    assert segment.text == VERSCHACHTELT

    ausgabe = adapter.apply_replacements(
        dirty_docx,
        {
            segment.id: [
                _replace_all(segment.text, "Andreas Meier", "[[PERSON_1]]"),
                _replace_all(segment.text, "Beat Vogel", "[[PERSON_2]]"),
            ]
        },
    )

    treffer = _segment_with(adapter.extract_segments(ausgabe), "Quelle:")
    assert treffer.text == "Quelle: [[PERSON_1]] (bestätigt durch [[PERSON_2]])"

    # Die Struktur bleibt erhalten: Hyperlink und w:ins stehen noch.
    with zipfile.ZipFile(io.BytesIO(ausgabe)) as zf:
        roh = zf.read("word/document.xml").decode("utf-8")
    assert "w:hyperlink" in roh
    assert "w:ins " in roh or "<w:ins" in roh


def test_xml_space_preserve_wird_gesetzt(adapter, dirty_docx):
    """Entsteht durch die Ersetzung ein Rand-Leerzeichen, muss es geschützt werden.

    Die Tabellenzelle enthält „Andreas Meier" in einem ``w:t`` **ohne**
    ``xml:space``. Wird nur „Meier" gelöscht, bleibt „Andreas " mit
    Leerzeichen am Ende übrig. Ohne ``xml:space="preserve"`` darf Word dieses
    Leerzeichen wegfallen lassen.
    """
    segments = adapter.extract_segments(dirty_docx)
    zelle = {s.text: s for s in segments}[ZELLE_A]

    with zipfile.ZipFile(io.BytesIO(dirty_docx)) as zf:
        vorher = etree.fromstring(zf.read("word/document.xml"))
    quelle = [
        node
        for node in vorher.iter(f"{{{W}}}t")
        if node.text == ZELLE_A
    ]
    assert quelle, "Vorbedingung: Die Zelle liegt in genau einem w:t"
    assert all(
        "{http://www.w3.org/XML/1998/namespace}space" not in node.attrib
        for node in quelle
    ), "Vorbedingung: dieses w:t hat noch kein xml:space"

    # Nur den Nachnamen entfernen — übrig bleibt "Andreas " samt Leerzeichen.
    ausgabe = adapter.apply_replacements(
        dirty_docx, {zelle.id: [_replace_all(zelle.text, "Meier", "")]}
    )
    with zipfile.ZipFile(io.BytesIO(ausgabe)) as zf:
        nachher = etree.fromstring(zf.read("word/document.xml"))
    mit_leerzeichen = [
        node for node in nachher.iter(f"{{{W}}}t") if node.text == "Andreas "
    ]
    assert mit_leerzeichen, "Der Rest 'Andreas ' fehlt"
    for node in mit_leerzeichen:
        assert (
            node.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
        ), "xml:space='preserve' fehlt — Word darf das Leerzeichen sonst tilgen"


# ── 3. Mehrere Ersetzungen in EINEM Segment ────────────────────────────────


def test_mehrere_ersetzungen_unterschiedlicher_laenge(adapter, dirty_docx):
    """Zwei Ersetzungen mit verschiedenen Längendifferenzen im selben Segment.

    Entscheidend ist, dass beide Treffer im **selben** ``w:t``-Knoten liegen —
    der Absatz besteht aus genau einem Run. Nur dann ist die Reihenfolge
    überhaupt beobachtbar: Über getrennte Runs hinweg berührt jede Ersetzung
    andere Knoten und jede Reihenfolge führt zum selben Ergebnis.

    Die Ersetzungen werden absichtlich in **aufsteigender** Reihenfolge
    übergeben. Würde der Adapter sie so anwenden, verschöbe die erste
    (13 Zeichen → 6) alle folgenden Offsets um −7 und die zweite schnitte
    mitten in „Beat Vogel" hinein.
    """
    segments = adapter.extract_segments(dirty_docx)
    segment = _segment_with(segments, "selben Abteilung")
    assert segment.text == EIN_RUN_SATZ

    # Vorbedingung: wirklich nur ein Run mit Text.
    document = Document(io.BytesIO(dirty_docx))
    ein_run_absatz = next(p for p in document.paragraphs if p.text == EIN_RUN_SATZ)
    assert len([r for r in ein_run_absatz.runs if r.text]) == 1

    erste = _replace_all(segment.text, "Andreas Meier", "[[P1]]")  # 13 -> 6
    zweite = _replace_all(segment.text, "Beat Vogel", "[[PERSON_2_LANG]]")  # 10 -> 17
    assert erste.start < zweite.start

    ausgabe = adapter.apply_replacements(dirty_docx, {segment.id: [erste, zweite]})

    treffer = _segment_with(adapter.extract_segments(ausgabe), "selben Abteilung")
    assert treffer.text == (
        "[[P1]] und [[PERSON_2_LANG]] gehören zur selben Abteilung."
    )

    # Zusätzlich der run-übergreifende Fall im anderen Absatz: hier beweist der
    # Test nicht die Reihenfolge, sondern dass beide Wege zusammenspielen.
    satz = _segment_with(segments, "wohnt in Bern")
    ausgabe2 = adapter.apply_replacements(
        dirty_docx,
        {
            satz.id: [
                _replace_all(satz.text, "Andreas Meier", "[[PERSON_1]]"),
                _replace_all(satz.text, "Beat Vogel", "[[PERSON_2_LANG]]"),
            ]
        },
    )
    assert _segment_with(adapter.extract_segments(ausgabe2), "wohnt in Bern").text == (
        "[[PERSON_1]] wohnt in Bern und arbeitet mit [[PERSON_2_LANG]] zusammen."
    )


def test_ueberlappende_ersetzungen_werden_abgelehnt(adapter, dirty_docx):
    """Überlappende Ersetzungen sind ein Programmierfehler und müssen knallen."""
    segments = adapter.extract_segments(dirty_docx)
    segment = _segment_with(segments, "wohnt in Bern")

    with pytest.raises(ValueError, match="Überlappende"):
        adapter.apply_replacements(
            dirty_docx,
            {
                segment.id: [
                    Replacement(0, 13, "[[A]]"),
                    Replacement(8, 20, "[[B]]"),
                ]
            },
        )


def test_einfuegung_neben_tabulator_an_run_grenze(adapter):
    """Eine reine Einfügung (``start == end``) direkt neben einem ``w:tab``.

    ``w:tab`` erzeugt im Segmenttext ein Zeichen, ist selbst aber nicht
    beschreibbar (kein ``w:t``). Liegt die Einfügeposition exakt an der
    Grenze zu einem solchen Knoten — vor einem führenden Tabulator oder nach
    einem schliessenden —, gibt es keine echte Überlappung und keinen
    beschreibbaren Knoten, dessen ``[start, end]``-Bereich die Position genau
    trifft. Der nächstgelegene beschreibbare Run muss den Text trotzdem
    aufnehmen, statt dass die Ersetzung mit einem ``ValueError`` abbricht.
    """
    # (a) Absatz "\tHallo Welt" — Tabulator vor dem Text, Einfügung an Position 0.
    führt = Document()
    p1 = führt.add_paragraph()
    p1.add_run().add_tab()
    p1.add_run("Hallo Welt")
    buffer1 = io.BytesIO()
    führt.save(buffer1)
    daten1 = buffer1.getvalue()

    segment1 = adapter.extract_segments(daten1)[0]
    assert segment1.text == "\tHallo Welt"

    ausgabe1 = adapter.apply_replacements(
        daten1, {segment1.id: [Replacement(0, 0, "XX-")]}
    )
    treffer1 = adapter.extract_segments(ausgabe1)[0]
    assert treffer1.text == "\tXX-Hallo Welt"

    # (b) Absatz "Hallo Welt\t" — Tabulator nach dem Text, Einfügung am Ende.
    folgt = Document()
    p2 = folgt.add_paragraph()
    p2.add_run("Hallo Welt")
    p2.add_run().add_tab()
    buffer2 = io.BytesIO()
    folgt.save(buffer2)
    daten2 = buffer2.getvalue()

    segment2 = adapter.extract_segments(daten2)[0]
    assert segment2.text == "Hallo Welt\t"

    ausgabe2 = adapter.apply_replacements(
        daten2, {segment2.id: [Replacement(11, 11, "-YY")]}
    )
    treffer2 = adapter.extract_segments(ausgabe2)[0]
    assert treffer2.text == "Hallo Welt-YY\t"


# ── 4. Kopf-/Fusszeile und Tabellenzellen ──────────────────────────────────


def test_kopfzeile_fusszeile_und_tabellenzellen(adapter, dirty_docx):
    """Alle vier Container werden erfasst und lassen sich ersetzen."""
    segments = adapter.extract_segments(dirty_docx)
    nach_text = {s.text: s for s in segments}

    kopf = nach_text[KOPFZEILE]
    fuss = nach_text[FUSSZEILE]
    zelle_a = nach_text[ZELLE_A]
    zelle_c = nach_text[ZELLE_C]

    # Kopf- und Fusszeile liegen in eigenen Parts …
    assert kopf.location.part_name.startswith("word/header")
    assert fuss.location.part_name.startswith("word/footer")
    # … und werden von python-docx erreicht, tragen also ein sprechendes Label.
    assert kopf.id.startswith("header0/")
    assert fuss.id.startswith("footer0/")
    # Tabellenzellen ebenso, mit Zeilen-/Spaltenangabe in der ID.
    assert zelle_a.id.startswith("body/t0/r0/c0/")
    assert zelle_c.id.startswith("body/t0/r1/c0/")

    ausgabe = adapter.apply_replacements(
        dirty_docx,
        {
            kopf.id: [_replace_all(kopf.text, "Andreas Meier", "[[PERSON_1]]")],
            fuss.id: [_replace_all(fuss.text, "Beat Vogel", "[[PERSON_2]]")],
            zelle_a.id: [_replace_all(zelle_a.text, "Andreas Meier", "[[PERSON_1]]")],
            zelle_c.id: [_replace_all(zelle_c.text, "Beat Vogel", "[[PERSON_2]]")],
        },
    )

    texte = _all_text(ausgabe)
    assert "Kanzlei Muster, zuständig: [[PERSON_1]]" in texte
    assert "Seite 1 von 1, erstellt von [[PERSON_2]]" in texte
    assert "[[PERSON_1]]" in texte  # Zelle A
    assert "[[PERSON_2]]" in texte  # Zelle C
    # Die unberührten Zellen sind unverändert.
    assert ZELLE_B in texte and ZELLE_D in texte

    # Und über python-docx gegengelesen.
    document = Document(io.BytesIO(ausgabe))
    assert document.tables[0].cell(0, 0).text == "[[PERSON_1]]"
    assert document.tables[0].cell(1, 0).text == "[[PERSON_2]]"
    assert document.sections[0].header.paragraphs[0].text.endswith("[[PERSON_1]]")
    assert document.sections[0].footer.paragraphs[0].text.endswith("[[PERSON_2]]")


# ── 5. Der Beweis für den lxml-Fallback ────────────────────────────────────


def test_fussnote_wird_nur_dank_lxml_fallback_erfasst(adapter, dirty_docx):
    """``word/footnotes.xml`` ist für python-docx unsichtbar — für uns nicht."""
    # Nachweis der Prämisse: python-docx erreicht diesen Text nicht.
    document = Document(io.BytesIO(dirty_docx))
    sichtbar_fuer_python_docx = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                sichtbar_fuer_python_docx.extend(p.text for p in cell.paragraphs)
    section = document.sections[0]
    sichtbar_fuer_python_docx.extend(p.text for p in section.header.paragraphs)
    sichtbar_fuer_python_docx.extend(p.text for p in section.footer.paragraphs)
    assert not any(FUSSNOTE_TEXT.strip() in t for t in sichtbar_fuer_python_docx)

    # Der Adapter findet ihn trotzdem — und markiert ihn als Fallback-Fund.
    segments = adapter.extract_segments(dirty_docx)
    fussnote = _segment_with(segments, "protokolliert am 3. Mai")
    assert fussnote.location.part_name == "word/footnotes.xml"
    assert fussnote.id.startswith("fallback:word/footnotes.xml/")

    ausgabe = adapter.apply_replacements(
        dirty_docx,
        {fussnote.id: [_replace_all(fussnote.text, "Andreas Meier", "[[PERSON_1]]")]},
    )

    with zipfile.ZipFile(io.BytesIO(ausgabe)) as zf:
        roh = zf.read("word/footnotes.xml").decode("utf-8")
    assert "[[PERSON_1]]" in roh
    assert "Andreas Meier" not in roh

    # Das Ergebnis bleibt ein gültiges DOCX.
    assert Document(io.BytesIO(ausgabe)).paragraphs


def test_textbox_wird_nur_dank_lxml_fallback_erfasst(adapter, dirty_docx):
    """``w:txbxContent`` hängt in einem Run und ist für python-docx unsichtbar."""
    document = Document(io.BytesIO(dirty_docx))
    assert all(TEXTBOX_TEXT not in p.text for p in document.paragraphs)

    segments = adapter.extract_segments(dirty_docx)
    textbox = _segment_with(segments, "Randnotiz")
    assert textbox.location.part_name == "word/document.xml"
    assert textbox.id.startswith("fallback:word/document.xml/")
    assert textbox.text == TEXTBOX_TEXT

    # Der umgebende Absatz enthält den Textbox-Text NICHT — sonst wäre dieselbe
    # Stelle in zwei Segmenten und würde zweimal ersetzt.
    anhang = _segment_with(segments, "Anhang:")
    assert anhang.text == "Anhang:"

    ausgabe = adapter.apply_replacements(
        dirty_docx,
        {textbox.id: [_replace_all(textbox.text, "Andreas Meier", "[[PERSON_1]]")]},
    )
    neu = _segment_with(adapter.extract_segments(ausgabe), "Randnotiz")
    assert neu.text == "Randnotiz: [[PERSON_1]] war nicht anwesend."


# ── 6. Metadaten ───────────────────────────────────────────────────────────


def test_strip_metadata_entfernt_autor_und_firma(adapter, dirty_docx):
    """Autor und Firma sind vorher nachweislich da und nachher nachweislich weg."""
    vorher = Document(io.BytesIO(dirty_docx))
    assert vorher.core_properties.author == AUTOR
    assert vorher.core_properties.last_modified_by == AUTOR
    assert vorher.core_properties.title == "Interner Entwurf"

    with zipfile.ZipFile(io.BytesIO(dirty_docx)) as zf:
        app_vorher = zf.read("docProps/app.xml").decode("utf-8")
    assert FIRMA in app_vorher

    ausgabe = adapter.strip_metadata(dirty_docx)

    nachher = Document(io.BytesIO(ausgabe))
    assert nachher.core_properties.author == ""
    assert nachher.core_properties.last_modified_by == ""
    assert nachher.core_properties.title == ""

    with zipfile.ZipFile(io.BytesIO(ausgabe)) as zf:
        app_nachher = zf.read("docProps/app.xml").decode("utf-8")
        core_nachher = zf.read("docProps/core.xml").decode("utf-8")
    assert FIRMA not in app_nachher
    assert AUTOR not in core_nachher
    # Die Elemente bleiben bestehen, sie sind nur leer.
    assert "<Company" in app_nachher

    # Der Inhalt bleibt vollständig erhalten.
    assert _all_text(ausgabe) == _all_text(dirty_docx)


# ── Registry ───────────────────────────────────────────────────────────────


def test_registry_liefert_docx_adapter():
    """Dateiname und MIME-Typ führen beide zum DOCX-Adapter."""
    assert isinstance(get_adapter("Einvernahme.docx"), DocxAdapter)
    assert isinstance(
        get_adapter(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        DocxAdapter,
    )


def test_defektes_paket_wird_klar_abgewiesen(adapter):
    """Eine Nicht-ZIP-Datei ergibt eine verständliche deutsche Fehlermeldung."""
    with pytest.raises(ValueError, match="kein gültiges DOCX"):
        adapter.extract_segments(b"Das ist kein DOCX, sondern reiner Text.")


def test_unberuehrte_parts_bleiben_byte_identisch(adapter, dirty_docx):
    """Was nicht ersetzt wird, wird auch nicht neu geschrieben.

    Nur so lässt sich zusichern, dass Bilder, Themes, Nummerierung und
    Formatvorlagen den Durchlauf unverändert überstehen — eine Neuserialisierung
    ändert Whitespace, Attributreihenfolge und Namensraum-Präfixe.
    """
    def geaenderte_parts(ausgabe: bytes) -> List[str]:
        with zipfile.ZipFile(io.BytesIO(dirty_docx)) as vorher, zipfile.ZipFile(
            io.BytesIO(ausgabe)
        ) as nachher:
            assert vorher.namelist() == nachher.namelist(), "Part-Liste verändert"
            return [
                name
                for name in vorher.namelist()
                if vorher.read(name) != nachher.read(name)
            ]

    segments = adapter.extract_segments(dirty_docx)

    # (a) Ersetzung im Hauptdokument — die Fussnote darf kein Byte verlieren.
    satz = _segment_with(segments, "wohnt in Bern")
    nur_body = adapter.apply_replacements(
        dirty_docx,
        {satz.id: [_replace_all(satz.text, "Andreas Meier", "[[PERSON_1]]")]},
    )
    assert geaenderte_parts(nur_body) == ["word/document.xml"]

    # (b) Ersetzung in der Fussnote — umgekehrt genauso.
    fussnote = _segment_with(segments, "protokolliert am 3. Mai")
    nur_fussnote = adapter.apply_replacements(
        dirty_docx,
        {fussnote.id: [_replace_all(fussnote.text, "Andreas Meier", "[[PERSON_1]]")]},
    )
    assert geaenderte_parts(nur_fussnote) == ["word/footnotes.xml"]


def test_konsistenzpruefung_bei_abweichendem_absatz():
    """Passt der Absatz nicht zum Segmenttext, wird abgebrochen statt geraten.

    Die Prüfung ist ein Sicherheitsnetz gegen einen künftigen Umbau, bei dem
    Segmente und Bytes auseinanderlaufen könnten — deshalb hier direkt gegen
    die interne Funktion getestet.
    """
    from backend.adapters.docx_adapter import _apply_to_paragraph

    paragraph = parse_xml(
        f'<w:p xmlns:w="{W}"><w:r><w:t>Andreas Meier</w:t></w:r></w:p>'
    )
    with pytest.raises(ValueError, match="passt nicht mehr zum Segment"):
        _apply_to_paragraph(
            paragraph, "Ein ganz anderer Text", [Replacement(0, 3, "X")]
        )


def test_unbekannte_segment_id_wird_ignoriert(adapter, dirty_docx):
    """IDs, die es nicht (mehr) gibt, dürfen nicht zum Absturz führen."""
    ersetzungen: Dict[str, List[Replacement]] = {
        "gibt-es-nicht/p99": [Replacement(0, 1, "X")]
    }
    ausgabe = adapter.apply_replacements(dirty_docx, ersetzungen)
    assert _all_text(ausgabe) == _all_text(dirty_docx)
