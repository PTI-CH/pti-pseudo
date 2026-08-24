"""Tests des XLSX-Adapters.

Der schwerste Fall steht in :func:`test_gemeinsame_zeichenkette_wird_nicht_geteilt`
— alles andere ist Handwerk. Alle Testdaten sind synthetisch; echte
Personendaten gehören nach Hausregel nicht ins Repository.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.adapters import Replacement, get_adapter
from backend.adapters.xlsx_adapter import XlsxAdapter
from backend.tests.ooxml_fabrik import baue_xlsx


def _parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.fixture
def adapter() -> XlsxAdapter:
    return XlsxAdapter()


@pytest.fixture
def mappe() -> bytes:
    """Zwei Blätter; ``Nachname`` steht zweimal und teilt sich einen Eintrag."""
    return baue_xlsx(
        [
            (
                "Personen",
                [
                    ["Nachname", "Vorname", "Ort"],
                    ["Meier", "Andrea", "Bern"],
                    ["Kunz", "Beat", "Chur"],
                ],
            ),
            ("Notizen", [["Nachname", "steht hier als Ueberschrift"]]),
        ]
    )


# ── Extraktion ─────────────────────────────────────────────────────────────


def test_ein_segment_je_zeile_mit_blattnamen(adapter: XlsxAdapter, mappe: bytes) -> None:
    """Die Segment-ID nennt Blatt und Excel-Zeilennummer."""
    segmente = adapter.extract_segments(mappe)
    ids = [s.id for s in segmente]
    assert ids == ["Personen/z1", "Personen/z2", "Personen/z3", "Notizen/z1"]
    assert segmente[1].text == "Meier\tAndrea\tBern"


def test_extraktion_ist_deterministisch(adapter: XlsxAdapter, mappe: bytes) -> None:
    """Zweimal dieselben Bytes muessen dieselben IDs und Texte liefern.

    Die Determinismus-Pflicht aus ``adapters/base.py``: Sonst zeigen die
    Offsets aus dem Analyse-Request beim Anwendungs-Request ins Leere.
    """
    erste = adapter.extract_segments(mappe)
    zweite = adapter.extract_segments(mappe)
    assert [(s.id, s.text) for s in erste] == [(s.id, s.text) for s in zweite]


def test_leere_zellen_erzeugen_keinen_tabulator(adapter: XlsxAdapter) -> None:
    """Luecken in der Zeile duerfen den Segmenttext nicht mit Rauschen fuellen.

    Stuende ein Name in Spalte Z einer sonst leeren Zeile, bestuende das
    Segment sonst aus 25 Tabulatoren und dem Namen — das Sprachmodell saehe
    keinen Satz, sondern eine Wueste.
    """
    daten = baue_xlsx([("Blatt", [["Meier", "", "Bern"]])])
    (segment,) = adapter.extract_segments(daten)
    assert segment.text == "Meier\tBern"


# ── Der Kernfall: gemeinsame Zeichenketten ─────────────────────────────────


def test_gemeinsame_zeichenkette_wird_nicht_geteilt(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Eine Ersetzung darf **nur** ihre eigene Zelle treffen.

    ``Nachname`` steht als Spaltenueberschrift auf Blatt "Personen" und
    nochmals auf Blatt "Notizen" — im XML teilen sich beide Zellen denselben
    Eintrag in ``xl/sharedStrings.xml``. Wer diesen Eintrag ueberschriebe,
    aenderte stillschweigend auch die andere Stelle.

    Das ist kein theoretischer Fall: Excel dedupliziert von sich aus, ein
    Nachname in einer Datenzeile und derselbe Nachname in einer Ueberschrift
    landen zwangslaeufig auf demselben Eintrag.
    """
    neu = adapter.apply_replacements(
        mappe, {"Personen/z1": [Replacement(0, len("Nachname"), "[[ORGANIZATION_1]]")]}
    )

    segmente = {s.id: s.text for s in adapter.extract_segments(neu)}
    assert segmente["Personen/z1"] == "[[ORGANIZATION_1]]\tVorname\tOrt"
    assert segmente["Notizen/z1"] == "Nachname\tsteht hier als Ueberschrift"

    # Und der Beweis auf Paketebene: die Tabelle selbst ist unberuehrt.
    assert _parts(neu)["xl/sharedStrings.xml"] == _parts(mappe)["xl/sharedStrings.xml"]


# ── Ersetzung ──────────────────────────────────────────────────────────────


def test_roundtrip_ohne_ersetzungen_aendert_nichts(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Leeres Ersetzungs-Dict liefert ein inhaltlich identisches Dokument."""
    neu = adapter.apply_replacements(mappe, {})
    assert [(s.id, s.text) for s in adapter.extract_segments(neu)] == [
        (s.id, s.text) for s in adapter.extract_segments(mappe)
    ]


def test_ersetzung_ueber_eine_zellgrenze(adapter: XlsxAdapter) -> None:
    """Ein Fund darf ueber den Tabulator laufen; die erste Zelle nimmt ihn auf.

    Die Erkennung sieht den verbundenen Zeilentext, also kann ein Name aus
    zwei Zellen bestehen ("Meier" | "Andrea"). Dieselbe Regel wie bei den
    Runs im DOCX-Adapter: Der Ersatztext geht vollstaendig in die erste
    beruehrte Zelle, die weiteren verlieren ihren ueberlappenden Teil.
    """
    daten = baue_xlsx([("Blatt", [["Meier", "Andrea", "Bern"]])])
    (segment,) = adapter.extract_segments(daten)
    assert segment.text == "Meier\tAndrea\tBern"

    neu = adapter.apply_replacements(
        daten, {segment.id: [Replacement(0, len("Meier\tAndrea"), "[[PERSON_1]]")]}
    )
    (nachher,) = adapter.extract_segments(neu)
    assert nachher.text == "[[PERSON_1]]\tBern"


def test_formel_wird_beim_ersetzen_entfernt(adapter: XlsxAdapter) -> None:
    """Bliebe die Formel stehen, holte die Neuberechnung den Klarnamen zurueck.

    Excel speichert zu einer Formel ihr letztes Ergebnis. Nur dieses zu
    ersetzen sieht richtig aus, bis jemand die Mappe oeffnet und Excel neu
    rechnet — dann steht der Name wieder da, im vermeintlich
    pseudonymisierten Dokument.
    """
    daten = baue_xlsx(
        [("Blatt", [["Ergebnis", "Andrea Meier"]])], formel_zelle=(0, 0, 1, "Andrea Meier")
    )
    (segment,) = adapter.extract_segments(daten)
    assert "Andrea Meier" in segment.text

    start = segment.text.index("Andrea Meier")
    neu = adapter.apply_replacements(
        daten,
        {segment.id: [Replacement(start, start + len("Andrea Meier"), "[[PERSON_1]]")]},
    )

    blatt = _parts(neu)["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert "[[PERSON_1]]" in blatt
    assert "CONCATENATE" not in blatt, "die Formel muss verschwunden sein"
    assert "Andrea Meier" not in blatt


def test_unbekannte_segment_id_wird_ignoriert(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Laut Schnittstellenvertrag ignorieren, nicht scheitern."""
    neu = adapter.apply_replacements(
        mappe, {"gibtsnicht/z9": [Replacement(0, 1, "X")]}
    )
    assert adapter.extract_segments(neu)[0].text.startswith("Nachname")


def test_ueberlappende_ersetzungen_werden_abgelehnt(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Stillschweigend eine verwerfen waere schlimmer — dann bliebe PII stehen."""
    with pytest.raises(ValueError, match="berlappende"):
        adapter.apply_replacements(
            mappe,
            {"Personen/z2": [Replacement(0, 5, "[[A]]"), Replacement(3, 8, "[[B]]")]},
        )


# ── Metadaten und Format ───────────────────────────────────────────────────


def test_strip_metadata_leert_autor_und_firma(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Der Fliesstext nuetzt nichts, wenn die Eigenschaften den Autor nennen."""
    neu = adapter.strip_metadata(mappe)
    core = _parts(neu)["docProps/core.xml"].decode("utf-8")
    app = _parts(neu)["docProps/app.xml"].decode("utf-8")
    assert "Regula Testperson" not in core
    assert "Regula Testperson" not in app
    assert "Musterstelle" not in app
    # Geleert, nicht entfernt — manche Programme erwarten die Elemente.
    assert "creator" in core


def test_unberuehrte_parts_bleiben_byte_identisch(
    adapter: XlsxAdapter, mappe: bytes
) -> None:
    """Was der Adapter nicht anfasst, kann er auch nicht kaputt machen."""
    neu = adapter.apply_replacements(
        mappe, {"Personen/z2": [Replacement(0, 5, "[[PERSON_1]]")]}
    )
    vorher, nachher = _parts(mappe), _parts(neu)
    for name in vorher:
        if name == "xl/worksheets/sheet1.xml":
            continue
        assert vorher[name] == nachher[name], f"{name} wurde unnoetig veraendert"


def test_kaputtes_zip_liefert_klare_meldung(adapter: XlsxAdapter) -> None:
    with pytest.raises(ValueError, match="XLSX"):
        adapter.extract_segments(b"keine zip-datei")


def test_registry_findet_den_adapter() -> None:
    """Ueber Dateiname und ueber MIME-Typ."""
    assert type(get_adapter("Mappe.xlsx")).__name__ == "XlsxAdapter"
    assert (
        type(
            get_adapter(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        ).__name__
        == "XlsxAdapter"
    )
