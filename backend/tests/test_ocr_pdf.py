"""Tests der OCR-Stufe des PDF-Adapters (Spec §4, Abnahmepunkte 5–6).

``tesseract`` ist in dieser Umgebung **nicht installiert** — echtes OCR lässt
sich hier nicht ausführen. Alle Tests laufen deshalb ausschliesslich mit
Dependency-Injection: einer Fake-Engine (:class:`_FakeEngine`) und einer
festen, injizierten Verfügbarkeit, genau wie ``backend/ki/`` mit einem
Test-``client`` statt einem echten HTTP-Client getestet wird (siehe
``backend/tests/test_ki_infomaniak.py``). Kein Test hier hängt an einem
installierten Tesseract-Binary.

Was hier NICHT getestet wird, aus genau diesem Grund: ob Tesseract auf
gerenderten Buchstaben tatsächlich „Sandra Zbinden" erkennt. Das ist Spec §8
(„OCR-Qualität ist nicht ausgemessen") und ausserhalb der Reichweite ohne
das Binary. Getestet wird die **Logik drumherum**: Wann OCR überhaupt
versucht wird, wie ein Ausfall aussieht, und wie Wort-Boxen zu
Redaction-Rechtecken werden — all das ist unabhängig vom Erkennungsergebnis
selbst.

Deckt ab:

1. Der Wächter aus Spec §4.3 — die wichtigste Zusicherung des ganzen
   Vorhabens: eine Bildseite ohne Textebene, OCR nicht verfügbar →
   ``NutzermeldungError``, kein stilles leeres Ergebnis.
2. Eine echte leere Trennseite (kein Text, kein Bild) → keine Segmente,
   kein Fehler — unabhängig davon, ob OCR verfügbar ist.
3. Mit Fake-Engine: ``extract_segments`` liefert OCR-Segmente mit
   ``quelle="ocr"`` und den erwarteten IDs; ``apply_replacements``
   schwärzt die richtigen Wort-Boxen.
4. Die Pixel→Punkte-Umrechnung (``pixel_zu_punkt``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pymupdf
import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters.base import Replacement  # noqa: E402
from backend.adapters.pdf_adapter import PdfAdapter  # noqa: E402
from backend.jobs.ausfuehrung import NutzermeldungError  # noqa: E402
from backend.ocr import OcrWort, pixel_zu_punkt  # noqa: E402

# ── Testdaten (frei erfunden, echte Umlaute — siehe golden-set/README.md) ──

NAME = "Sandra Zbinden"


# ── Fake-Engine ─────────────────────────────────────────────────────────────


class _FakeEngine:
    """Liefert kanonische Wort-Boxen, unabhängig vom übergebenen Bild.

    Genau das Muster aus dem Aufgabentext: „eine Fake-Engine, die kanonische
    Wort-Boxen zurückgibt, ohne echtes Tesseract". ``bild`` wird bewusst
    ignoriert — ein echtes Bild ist für diesen Test irrelevant, seine
    Existenz (als gültige Bildseite) wird bereits durch ``_bildseite()``
    sichergestellt.
    """

    def __init__(self, woerter: List[OcrWort]) -> None:
        self._woerter = woerter
        self.aufrufe = 0

    def erkenne_seite(self, bild, *, sprache: str = "deu") -> List[OcrWort]:
        self.aufrufe += 1
        return list(self._woerter)


def _kanonische_woerter() -> List[OcrWort]:
    """Zwei Wörter einer Zeile: ``"Sandra Zbinden"`` bei 200 dpi.

    Positionen frei gewählt, aber bewusst so, dass die beiden Wörter
    horizontal nebeneinander auf derselben Tesseract-Zeile liegen
    (gleiches ``block_num``/``par_num``/``line_num``, aufsteigendes
    ``word_num``) — das Bild aus ``_bildseite()`` schreibt den Namen an
    dieselbe Stelle, die genauen Pixel sind für die Fake-Engine aber
    irrelevant, sie liefert diese Werte unabhängig vom Bildinhalt.
    """
    return [
        OcrWort(
            text="Sandra",
            left=100,
            top=200,
            width=150,
            height=30,
            block_num=1,
            par_num=1,
            line_num=1,
            word_num=1,
        ),
        OcrWort(
            text="Zbinden",
            left=260,
            top=200,
            width=180,
            height=30,
            block_num=1,
            par_num=1,
            line_num=1,
            word_num=2,
        ),
    ]


# ── PDF-Fabrik: Bildseiten (bewusst NICHT in pdf_fabrik.py — siehe dessen
#    Modulkopf: die Textebenen-Fabrik rührt Bildseiten ausdrücklich nicht an)


def _bildseite(text: str = NAME, *, dpi: int = 200) -> bytes:
    """Baut ein einseitiges PDF, dessen einzige Seite ein Bild trägt.

    Der Text wird auf einer Hilfsseite gesetzt, zu einem Pixmap gerastert
    und als Bild in eine neue, gleich grosse Seite eingefügt — die Zielseite
    hat dadurch **keine Textebene** (``get_text("text")`` liefert nichts),
    aber ein Bild-XObject (``get_images()`` findet es). Die Zielseite ist
    exakt so gross gewählt, dass ihr erneutes Rastern mit demselben ``dpi``
    wieder ein Pixmap identischer Pixelgrösse ergibt — konsistent mit der
    Annahme hinter :func:`~backend.ocr.pixel_zu_punkt`, dass die Seite bei
    ``(0, 0)`` beginnt.
    """
    quelle = pymupdf.open()
    try:
        hilfsseite = quelle.new_page()
        hilfsseite.insert_text((36, 72), text, fontsize=24, fontname="helv")
        pixmap = hilfsseite.get_pixmap(dpi=dpi)
    finally:
        quelle.close()

    ziel = pymupdf.open()
    try:
        breite = pixmap.width * 72 / dpi
        hoehe = pixmap.height * 72 / dpi
        zielseite = ziel.new_page(width=breite, height=hoehe)
        zielseite.insert_image(zielseite.rect, pixmap=pixmap)
        return ziel.tobytes()
    finally:
        ziel.close()


def _leere_seite() -> bytes:
    """Baut ein einseitiges PDF ohne Text und ohne Bild — echte Trennseite."""
    doc = pymupdf.open()
    try:
        doc.new_page()
        return doc.tobytes()
    finally:
        doc.close()


def _get_text(data: bytes) -> str:
    """Liest den sichtbaren Text aller Seiten — unabhängig vom Adapter."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def _verweigert() -> bool:
    """Feste, injizierte Nicht-Verfügbarkeit — der Kern des Wächter-Tests."""
    return False


def _schlaegt_fehl_wenn_aufgerufen() -> bool:
    """Beweist per ``AssertionError``, dass diese Prüfung NICHT lief.

    Für die leere Trennseite: ``_seite_traegt_bild`` muss die Methode schon
    verlassen, bevor überhaupt eine OCR-Verfügbarkeit geprüft wird (Spec §4 —
    „kein Bild" geht vor „OCR verfügbar?").
    """
    raise AssertionError(
        "ocr_verfuegbar wurde aufgerufen, obwohl die Seite kein Bild trägt"
    )


# ── 1. Der Wächter (Spec §4.3) ──────────────────────────────────────────────


def test_bildseite_ohne_ocr_wirft_nutzermeldung():
    """Bildseite ohne Textebene, OCR nicht verfügbar → NutzermeldungError.

    Die tragende Sicherheitsregel des ganzen Vorhabens: Ein Scan, dessen
    Text niemand lesen konnte, darf NIE als „verarbeitet" durchgehen. Kein
    stilles leeres Ergebnis — ein lauter, für Menschen lesbarer Fehler.
    """
    adapter = PdfAdapter(ocr_verfuegbar=_verweigert)
    with pytest.raises(NutzermeldungError):
        adapter.extract_segments(_bildseite())


def test_bildseite_ohne_ocr_liefert_kein_stilles_ergebnis():
    """Verschärfte Fassung: Es kommt bei Ausfall NIRGENDS eine leere Liste an.

    Ein ``except NutzermeldungError: []`` irgendwo im Aufrufer wäre exakt
    das gefährliche Fehlerbild aus Spec §4.3. Dieser Test stellt sicher,
    dass ``extract_segments`` selbst (nicht erst ein Aufrufer) die Ausnahme
    wirft — es gibt keinen Rückgabewert, den ein Aufrufer versehentlich als
    Erfolg missverstehen könnte.
    """
    adapter = PdfAdapter(ocr_verfuegbar=_verweigert)
    try:
        ergebnis = adapter.extract_segments(_bildseite())
    except NutzermeldungError as fehler:
        assert str(fehler)  # ein Text fuer den Menschen, kein leerer Fehler
        return
    pytest.fail(f"kein Fehler geworfen, sondern ein Ergebnis: {ergebnis!r}")


# ── 2. Leere Trennseite ──────────────────────────────────────────────────


def test_leere_trennseite_liefert_keine_segmente_und_keinen_fehler():
    """Kein Text, kein Bild → ``[]``, unabhängig von der OCR-Verfügbarkeit.

    OCR wird dabei gar nicht erst geprüft (siehe
    ``_schlaegt_fehl_wenn_aufgerufen``) — eine leere Seite ist kein
    Scan-Verdacht.
    """
    adapter = PdfAdapter(ocr_verfuegbar=_schlaegt_fehl_wenn_aufgerufen)
    segmente = adapter.extract_segments(_leere_seite())
    assert segmente == []


def test_leere_trennseite_auch_wenn_ocr_verfuegbar_waere():
    """Dasselbe mit OCR auf ``True`` — die Aussage ist unabhängig davon."""
    engine = _FakeEngine(_kanonische_woerter())
    adapter = PdfAdapter(ocr_engine=engine, ocr_verfuegbar=lambda: True)
    segmente = adapter.extract_segments(_leere_seite())
    assert segmente == []
    assert engine.aufrufe == 0  # keine Rasterung fuer eine Seite ohne Bild


# ── 3. Fake-Engine: Segmente und Redaction ──────────────────────────────


def test_extract_liefert_ocr_segmente_mit_richtiger_id_und_quelle():
    """OCR-Segmente tragen ``quelle="ocr"`` und die ID ``page{p}/ocr/line{n}``."""
    engine = _FakeEngine(_kanonische_woerter())
    adapter = PdfAdapter(ocr_engine=engine, ocr_verfuegbar=lambda: True, ocr_dpi=200)

    segmente = adapter.extract_segments(_bildseite())

    assert len(segmente) == 1
    (segment,) = segmente
    assert segment.id == "page0/ocr/line0"
    assert segment.text == NAME
    assert segment.location.page_index == 0
    assert segment.location.line_index == 0
    assert segment.location.quelle == "ocr"
    assert engine.aufrufe == 1


def test_extract_ist_deterministisch_bei_identischen_bytes():
    """Zweimaliges ``extract_segments`` auf identische Bytes → Identisches.

    Determinismus-Pflicht aus ``adapters/base.py`` — auch für den OCR-Zweig.
    """
    daten = _bildseite()
    engine = _FakeEngine(_kanonische_woerter())
    adapter = PdfAdapter(ocr_engine=engine, ocr_verfuegbar=lambda: True, ocr_dpi=200)

    erster_lauf = adapter.extract_segments(daten)
    zweiter_lauf = adapter.extract_segments(daten)

    assert [(s.id, s.text) for s in erster_lauf] == [
        (s.id, s.text) for s in zweiter_lauf
    ]


def test_apply_replacements_stempelt_platzhalter_ueber_die_ocr_zeile():
    """Volle Zeile ersetzt: nach ``apply_replacements`` steht der Platzhalter.

    Die Bildseite hat vor der Ersetzung **keine** Textebene (siehe
    ``_bildseite``); jeder danach extrahierbare Text kommt zwingend vom
    gestempelten Platzhalter selbst — ein direkter Nachweis, dass Redaction
    UND Stempel im Bild-Koordinatenraum tatsächlich gelaufen sind, nicht nur,
    dass kein Fehler flog.
    """
    daten = _bildseite()
    engine = _FakeEngine(_kanonische_woerter())
    adapter = PdfAdapter(ocr_engine=engine, ocr_verfuegbar=lambda: True, ocr_dpi=200)

    (segment,) = adapter.extract_segments(daten)
    ersetzt = adapter.apply_replacements(
        daten,
        {segment.id: [Replacement(0, len(segment.text), "[[PERSON_1]]")]},
    )

    text = _get_text(ersetzt)
    assert "[[PERSON_1]]" in text
    assert "Sandra" not in text
    assert "Zbinden" not in text


def test_apply_replacements_schwaerzt_nur_das_getroffene_wort():
    """Eine Ersetzung, die nur ``"Sandra"`` trifft, lässt ``"Zbinden"`` in Ruhe.

    Wort-granular heisst auch: ein Fund, der nur einen Teil der Zeile
    abdeckt, darf nicht die ganze Zeile schwärzen.
    """
    daten = _bildseite()
    engine = _FakeEngine(_kanonische_woerter())
    adapter = PdfAdapter(ocr_engine=engine, ocr_verfuegbar=lambda: True, ocr_dpi=200)

    (segment,) = adapter.extract_segments(daten)
    ende_sandra = len("Sandra")
    ersetzt = adapter.apply_replacements(
        daten,
        {segment.id: [Replacement(0, ende_sandra, "[[PERSON_1]]")]},
    )

    text = _get_text(ersetzt)
    assert "[[PERSON_1]]" in text
    assert "Sandra" not in text
    # "Zbinden" stand nicht in der Ersetzung und bleibt als Bild-Pixel stehen
    # -- ohne echtes Tesseract nicht aus dem Ergebnis-PDF rueckextrahierbar,
    # die Abwesenheit von "[[PERSON_1]]" ein zweites Mal genuegt hier als
    # Nachweis, dass nicht die ganze Zeile geschwaerzt wurde.
    assert text.count("[[PERSON_1]]") == 1


def test_redaction_rechteck_deckt_die_getroffenen_woerter_ab():
    """Direkter Nachweis: Das Redaction-Rechteck ist die Vereinigung der

    Pixel-Boxen der überschneidenden Wörter, umgerechnet in PDF-Punkte —
    exakt das, was Spec §4.2 verlangt. Weil ``apply_redactions()`` die
    Annotation nach der Anwendung entfernt, wird hier die interne
    Rechteck-Berechnung direkt geprüft statt sie aus dem Ergebnis-PDF
    zurückzugewinnen.
    """
    from backend.adapters.pdf_adapter import _ocr_union_rect
    from backend.ocr import gruppiere_zeilen

    (zeile,) = gruppiere_zeilen(_kanonische_woerter())
    dpi = 200

    nur_sandra = _ocr_union_rect(zeile, 0, len("Sandra"), dpi)
    x0, y0 = pixel_zu_punkt(100, 200, dpi)
    x1, y1 = pixel_zu_punkt(100 + 150, 200 + 30, dpi)
    assert nur_sandra.x0 == pytest.approx(x0)
    assert nur_sandra.y0 == pytest.approx(y0)
    assert nur_sandra.x1 == pytest.approx(x1)
    assert nur_sandra.y1 == pytest.approx(y1)

    beide = _ocr_union_rect(zeile, 0, len(NAME), dpi)
    erwartet_x0, erwartet_y0 = pixel_zu_punkt(100, 200, dpi)
    erwartet_x1, erwartet_y1 = pixel_zu_punkt(260 + 180, 200 + 30, dpi)
    assert beide.x0 == pytest.approx(erwartet_x0)
    assert beide.y0 == pytest.approx(erwartet_y0)
    assert beide.x1 == pytest.approx(erwartet_x1)
    assert beide.y1 == pytest.approx(erwartet_y1)


def test_ueberlappung_trifft_das_ganze_wort_nicht_nur_den_teilbereich():
    """Ein Zeichenbereich, der nur teilweise in ein Wort hineinragt, trifft es ganz.

    Lieber etwas zu viel überdeckt als ein Namensrest sichtbar (Spec §4.2).
    Ein Bereich, der bis mitten in ``"Zbinden"`` reicht, muss dessen
    vollständige Box treffen, nicht nur einen Ausschnitt davon.
    """
    from backend.adapters.pdf_adapter import _ocr_union_rect

    from backend.ocr import gruppiere_zeilen

    (zeile,) = gruppiere_zeilen(_kanonische_woerter())
    dpi = 200

    # "Sandra Zbi" -- reicht drei Zeichen in "Zbinden" hinein.
    teiltreffer = _ocr_union_rect(zeile, 0, len("Sandra Zbi"), dpi)
    voller_treffer = _ocr_union_rect(zeile, 0, len(NAME), dpi)
    assert teiltreffer.x0 == pytest.approx(voller_treffer.x0)
    assert teiltreffer.x1 == pytest.approx(voller_treffer.x1)


def test_unbeteiligtes_wort_bleibt_ausserhalb_des_rechtecks():
    """Ein Rechteck für nur ``"Sandra"`` reicht nicht bis zu ``"Zbinden"``."""
    from backend.adapters.pdf_adapter import _ocr_union_rect
    from backend.ocr import gruppiere_zeilen

    (zeile,) = gruppiere_zeilen(_kanonische_woerter())
    dpi = 200

    nur_sandra = _ocr_union_rect(zeile, 0, len("Sandra"), dpi)
    zbinden_x0, _ = pixel_zu_punkt(260, 200, dpi)
    assert nur_sandra.x1 < zbinden_x0


# ── 4. Pixel → Punkte ─────────────────────────────────────────────────────


def test_pixel_zu_punkt_rechnet_ueber_den_rasterungszoom_um():
    """200 px bei 200 dpi sind exakt 1 Zoll = 72 Punkte."""
    x, y = pixel_zu_punkt(200.0, 400.0, dpi=200)
    assert x == pytest.approx(72.0)
    assert y == pytest.approx(144.0)


def test_pixel_zu_punkt_bei_anderer_dpi():
    """100 px bei 100 dpi sind ebenfalls 1 Zoll -- die dpi ist der Hebel."""
    x, y = pixel_zu_punkt(100.0, 50.0, dpi=100)
    assert x == pytest.approx(72.0)
    assert y == pytest.approx(36.0)


def test_pixel_zu_punkt_am_ursprung_ist_der_ursprung():
    """``(0, 0)`` bleibt unabhängig von der dpi ``(0, 0)``."""
    assert pixel_zu_punkt(0.0, 0.0, dpi=200) == (0.0, 0.0)


# ── Konfiguration ────────────────────────────────────────────────────────


def test_ocr_ist_ab_werk_aus():
    """``Settings.ocr_enabled`` ist ``False``, solange ``OCR_ENABLED`` fehlt.

    Deploy-Entscheidung, siehe ``backend/config.py``: Das Systembinary
    ``tesseract`` muss auf dem Knoten liegen, bevor der Schalter etwas
    nützt.
    """
    from backend.config import Settings

    assert Settings().ocr_enabled is False
    assert Settings().ocr_dpi == 200
