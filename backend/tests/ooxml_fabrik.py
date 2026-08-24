"""Baut minimale, gültige XLSX- und PPTX-Pakete für die Adaptertests.

Warum von Hand und nicht mit einer Bibliothek
---------------------------------------------
``openpyxl`` und ``python-pptx`` sind **keine** Abhängigkeiten dieses Projekts,
und zwar mit Absicht: Beide schreiben das Paket beim Speichern neu und
gefährden damit die Roundtrip-Zusage der Adapter (siehe ``adapters/ooxml.py``).
Sie nur für die Tests aufzunehmen hiesse, die Testdaten mit einem Werkzeug zu
erzeugen, dessen Verhalten der Produktionscode gerade vermeidet.

Der zweite, wichtigere Grund: Diese Fabrik erzeugt genau die Strukturen, an
denen ein Adapter scheitert — einen Namen über drei Runs verteilt, zwei Zellen
an **derselben** gemeinsamen Zeichenkette, eine Formel mit
zwischengespeichertem Ergebnis. Eine Bibliothek würde das teils
wegnormalisieren.

Die Pakete sind bewusst knapp: Sie enthalten, was der Standard verlangt und
die Adapter lesen, nicht mehr. Alle Werte sind synthetisch — echte
Personendaten gehören nach Hausregel nicht ins Repository.
"""

from __future__ import annotations

import io
import zipfile
from typing import Dict, Iterable, List, Sequence, Tuple

__all__ = ["baue_xlsx", "baue_pptx"]

_CONTENT_TYPES_XLSX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
{blaetter}
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_ROOT_RELS_XLSX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_CORE_PROPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:creator>Regula Testperson</dc:creator><cp:lastModifiedBy>Regula Testperson</cp:lastModifiedBy>
<dc:title>Interner Titel</dc:title></cp:coreProperties>"""

_APP_PROPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<Company>Musterstelle</Company><Manager>Regula Testperson</Manager></Properties>"""


def _zippe(parts: Dict[str, str]) -> bytes:
    """Packt die Parts in stabiler Reihenfolge zu einem ZIP."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(parts):
            zf.writestr(name, parts[name].encode("utf-8"))
    return puffer.getvalue()


# ── XLSX ───────────────────────────────────────────────────────────────────


def baue_xlsx(
    blaetter: Sequence[Tuple[str, Sequence[Sequence[str]]]],
    formel_zelle: Tuple[int, int, int, str] | None = None,
) -> bytes:
    """Baut eine Arbeitsmappe aus Blattnamen und Zeilen.

    Parameters
    ----------
    blaetter
        Je Blatt ``(name, zeilen)``, je Zeile eine Folge von Zelltexten.
        Ein leerer Zelltext erzeugt **keine** Zelle — so entstehen Lücken wie
        in echten Mappen.
    formel_zelle
        Optional ``(blatt_index, zeilen_index, spalten_index, ergebnis)``:
        Diese Zelle wird als Formelzelle mit zwischengespeichertem Ergebnis
        geschrieben (``t="str"`` plus ``x:f``). Damit lässt sich prüfen, dass
        der Adapter die Formel beim Ersetzen entfernt, statt den Klarnamen
        von der nächsten Neuberechnung zurückholen zu lassen.

    Alle Texte landen in ``xl/sharedStrings.xml`` und werden **dedupliziert** —
    genau das erzeugt den Fall, in dem zwei Zellen sich einen Eintrag teilen.
    """
    gemeinsame: List[str] = []
    index_von: Dict[str, int] = {}

    def si_index(text: str) -> int:
        if text not in index_von:
            index_von[text] = len(gemeinsame)
            gemeinsame.append(text)
        return index_von[text]

    parts: Dict[str, str] = {
        "_rels/.rels": _ROOT_RELS_XLSX,
        "docProps/core.xml": _CORE_PROPS,
        "docProps/app.xml": _APP_PROPS,
    }

    blatt_overrides: List[str] = []
    sheet_eintraege: List[str] = []
    rel_eintraege: List[str] = []

    for b_index, (blatt_name, zeilen) in enumerate(blaetter, start=1):
        part = f"xl/worksheets/sheet{b_index}.xml"
        zeilen_xml: List[str] = []
        for z_index, zeile in enumerate(zeilen, start=1):
            zellen_xml: List[str] = []
            for s_index, wert in enumerate(zeile):
                if not wert:
                    continue
                ref = f"{chr(ord('A') + s_index)}{z_index}"
                ist_formel = formel_zelle is not None and formel_zelle[:3] == (
                    b_index - 1,
                    z_index - 1,
                    s_index,
                )
                if ist_formel:
                    zellen_xml.append(
                        f'<c r="{ref}" t="str"><f>CONCATENATE(X1,Y1)</f>'
                        f"<v>{wert}</v></c>"
                    )
                else:
                    zellen_xml.append(
                        f'<c r="{ref}" t="s"><v>{si_index(wert)}</v></c>'
                    )
            if zellen_xml:
                zeilen_xml.append(f'<row r="{z_index}">{"".join(zellen_xml)}</row>')

        parts[part] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(zeilen_xml)}</sheetData></worksheet>'
        )
        blatt_overrides.append(
            f'<Override PartName="/{part}" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        sheet_eintraege.append(
            f'<sheet name="{blatt_name}" sheetId="{b_index}" r:id="rId{b_index}"/>'
        )
        rel_eintraege.append(
            f'<Relationship Id="rId{b_index}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{b_index}.xml"/>'
        )

    si_xml = "".join(f"<si><t>{t}</t></si>" for t in gemeinsame)
    parts["xl/sharedStrings.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(gemeinsame)}" uniqueCount="{len(gemeinsame)}">{si_xml}</sst>'
    )
    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheet_eintraege)}</sheets></workbook>'
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{"".join(rel_eintraege)}</Relationships>'
    )
    parts["[Content_Types].xml"] = _CONTENT_TYPES_XLSX.format(
        blaetter="".join(blatt_overrides)
    )
    return _zippe(parts)


# ── PPTX ───────────────────────────────────────────────────────────────────

_CONTENT_TYPES_PPTX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
{teile}
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_ROOT_RELS_PPTX = _ROOT_RELS_XLSX.replace("xl/workbook.xml", "ppt/presentation.xml")


def _absatz(runs: Iterable[str]) -> str:
    """Ein ``a:p`` aus mehreren Runs — der Fall, an dem naive Sucher scheitern."""
    inhalt = "".join(f'<a:r><a:rPr lang="de-CH"/><a:t>{r}</a:t></a:r>' for r in runs)
    return f"<a:p>{inhalt}</a:p>"


def baue_pptx(folien: Sequence[Tuple[Sequence[Sequence[str]], Sequence[str]]]) -> bytes:
    """Baut eine Präsentation aus Folien und Notizen.

    Parameters
    ----------
    folien
        Je Folie ``(absaetze, notizen)``. Ein Absatz ist eine Folge von
        **Run-Texten** — mehrere Runs erzeugen genau den Fall, in dem ein Name
        im XML zerschnitten ist. ``notizen`` ist eine Folge von Absatztexten;
        eine leere Folge erzeugt keinen Notizen-Part.
    """
    parts: Dict[str, str] = {
        "_rels/.rels": _ROOT_RELS_PPTX,
        "docProps/core.xml": _CORE_PROPS,
        "docProps/app.xml": _APP_PROPS,
    }
    overrides: List[str] = []
    sld_ids: List[str] = []
    praes_rels: List[str] = []

    for f_index, (absaetze, notizen) in enumerate(folien, start=1):
        folien_part = f"ppt/slides/slide{f_index}.xml"
        koerper = "".join(_absatz(runs) for runs in absaetze)
        parts[folien_part] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody>"
            f"{koerper}"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        overrides.append(
            f'<Override PartName="/{folien_part}" ContentType="application/'
            'vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        sld_ids.append(f'<p:sldId id="{255 + f_index}" r:id="rId{f_index}"/>')
        praes_rels.append(
            f'<Relationship Id="rId{f_index}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{f_index}.xml"/>'
        )

        if notizen:
            notiz_part = f"ppt/notesSlides/notesSlide{f_index}.xml"
            notiz_koerper = "".join(_absatz([t]) for t in notizen)
            parts[notiz_part] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:cSld><p:spTree><p:sp><p:txBody>"
                f"{notiz_koerper}"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
            )
            overrides.append(
                f'<Override PartName="/{notiz_part}" ContentType="application/'
                'vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
            )
            parts[f"ppt/slides/_rels/slide{f_index}.xml.rels"] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/notesSlide" '
                f'Target="../notesSlides/notesSlide{f_index}.xml"/></Relationships>'
            )

    parts["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<p:sldIdLst>{"".join(sld_ids)}</p:sldIdLst></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{"".join(praes_rels)}</Relationships>'
    )
    parts["[Content_Types].xml"] = _CONTENT_TYPES_PPTX.format(teile="".join(overrides))
    return _zippe(parts)
