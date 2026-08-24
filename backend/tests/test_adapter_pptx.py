"""Tests des PPTX-Adapters.

Zwei Dinge tragen diesen Adapter: der ueber Runs zerschnittene Name (dieselbe
Falle wie in Word) und die **Notizen** — der Ort, an dem in einer
Praesentation regelmaessig steht, was auf der Folie bewusst nicht steht.

Alle Testdaten sind synthetisch; echte Personendaten gehoeren nach Hausregel
nicht ins Repository.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.adapters import Replacement, get_adapter
from backend.adapters.pptx_adapter import PptxAdapter
from backend.tests.ooxml_fabrik import baue_pptx


def _parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.fixture
def adapter() -> PptxAdapter:
    return PptxAdapter()


@pytest.fixture
def praesentation() -> bytes:
    """Zwei Folien; auf Folie 1 ist der Name ueber vier Runs zerschnitten."""
    return baue_pptx(
        [
            (
                [["Es schrieb ", "Andrea ", "Bei", "spiel."], ["Zweiter Absatz."]],
                ["Intern: Der Fall betrifft Beat Kunz."],
            ),
            ([["Zusammenfassung"]], []),
        ]
    )


# ── Extraktion ─────────────────────────────────────────────────────────────


def test_runs_werden_zu_einem_segment_verbunden(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Der ueber Runs zerschnittene Name muss als ein Text ankommen.

    ``Andrea Beispiel`` steht im XML als ``["Andrea ", "Bei", "spiel."]``.
    Wer Run fuer Run analysiert, findet ihn nie.
    """
    segmente = {s.id: s.text for s in adapter.extract_segments(praesentation)}
    assert segmente["folie1/p0"] == "Es schrieb Andrea Beispiel."


def test_notizen_werden_gefunden(adapter: PptxAdapter, praesentation: bytes) -> None:
    """Der praktisch wichtigste Fundort nach den Folien."""
    segmente = {s.id: s.text for s in adapter.extract_segments(praesentation)}
    assert segmente["folie1/notizen/p0"] == "Intern: Der Fall betrifft Beat Kunz."


def test_segmentreihenfolge_folgt_der_praesentation(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Folie, ihre Notizen, dann die naechste Folie."""
    ids = [s.id for s in adapter.extract_segments(praesentation)]
    assert ids == [
        "folie1/p0",
        "folie1/p1",
        "folie1/notizen/p0",
        "folie2/p0",
    ]


def test_extraktion_ist_deterministisch(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Determinismus-Pflicht aus ``adapters/base.py``."""
    erste = adapter.extract_segments(praesentation)
    zweite = adapter.extract_segments(praesentation)
    assert [(s.id, s.text) for s in erste] == [(s.id, s.text) for s in zweite]


# ── Ersetzung ──────────────────────────────────────────────────────────────


def test_ersetzung_ueber_run_grenzen(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Der Ersatztext geht vollstaendig in den ersten beruehrten Run."""
    start = len("Es schrieb ")
    neu = adapter.apply_replacements(
        praesentation,
        {"folie1/p0": [Replacement(start, start + len("Andrea Beispiel"), "[[PERSON_1]]")]},
    )
    segmente = {s.id: s.text for s in adapter.extract_segments(neu)}
    assert segmente["folie1/p0"] == "Es schrieb [[PERSON_1]]."


def test_ersetzung_in_den_notizen(adapter: PptxAdapter, praesentation: bytes) -> None:
    """Was in den Notizen steht, muss ebenso ersetzbar sein wie die Folie."""
    text = "Intern: Der Fall betrifft Beat Kunz."
    start = text.index("Beat Kunz")
    neu = adapter.apply_replacements(
        praesentation,
        {"folie1/notizen/p0": [Replacement(start, start + len("Beat Kunz"), "[[PERSON_2]]")]},
    )
    segmente = {s.id: s.text for s in adapter.extract_segments(neu)}
    assert segmente["folie1/notizen/p0"] == "Intern: Der Fall betrifft [[PERSON_2]]."


def test_roundtrip_ohne_ersetzungen_aendert_nichts(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    neu = adapter.apply_replacements(praesentation, {})
    assert [(s.id, s.text) for s in adapter.extract_segments(neu)] == [
        (s.id, s.text) for s in adapter.extract_segments(praesentation)
    ]


def test_zeichenformatierung_bleibt_erhalten(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Die ``a:rPr`` der Runs duerfen nicht verlorengehen."""
    neu = adapter.apply_replacements(
        praesentation, {"folie1/p1": [Replacement(0, 7, "[[X]]")]}
    )
    folie = _parts(neu)["ppt/slides/slide1.xml"].decode("utf-8")
    assert 'lang="de-CH"' in folie


def test_unberuehrte_parts_bleiben_byte_identisch(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    """Was der Adapter nicht anfasst, kann er auch nicht kaputt machen."""
    neu = adapter.apply_replacements(
        praesentation, {"folie1/p0": [Replacement(0, 2, "[[X]]")]}
    )
    vorher, nachher = _parts(praesentation), _parts(neu)
    for name in vorher:
        if name == "ppt/slides/slide1.xml":
            continue
        assert vorher[name] == nachher[name], f"{name} wurde unnoetig veraendert"


def test_ueberlappende_ersetzungen_werden_abgelehnt(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    with pytest.raises(ValueError, match="berlappende"):
        adapter.apply_replacements(
            praesentation,
            {"folie1/p0": [Replacement(0, 5, "[[A]]"), Replacement(3, 8, "[[B]]")]},
        )


def test_unbekannte_segment_id_wird_ignoriert(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    neu = adapter.apply_replacements(
        praesentation, {"folie9/p0": [Replacement(0, 1, "X")]}
    )
    assert adapter.extract_segments(neu)[0].text == "Es schrieb Andrea Beispiel."


# ── Metadaten und Format ───────────────────────────────────────────────────


def test_strip_metadata_leert_autor_und_firma(
    adapter: PptxAdapter, praesentation: bytes
) -> None:
    neu = adapter.strip_metadata(praesentation)
    core = _parts(neu)["docProps/core.xml"].decode("utf-8")
    app = _parts(neu)["docProps/app.xml"].decode("utf-8")
    assert "Regula Testperson" not in core
    assert "Regula Testperson" not in app
    assert "Musterstelle" not in app


def test_kaputtes_zip_liefert_klare_meldung(adapter: PptxAdapter) -> None:
    with pytest.raises(ValueError, match="PPTX"):
        adapter.extract_segments(b"keine zip-datei")


def test_registry_findet_den_adapter() -> None:
    assert type(get_adapter("Folien.pptx")).__name__ == "PptxAdapter"
    assert (
        type(
            get_adapter(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            )
        ).__name__
        == "PptxAdapter"
    )
