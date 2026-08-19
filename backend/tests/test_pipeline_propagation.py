"""Tests der Fund-Uebertragung (``pipeline.propagate_findings``).

Der Anlass war ein Fehlerbild aus dem Betrieb: In der Befundliste stand
``LogObject`` als Fund, im heruntergeladenen Word-Dokument stand ``LogObject``
weiterhin im Klartext. Kein Fehler beim Zurueckschreiben — das NER-Modell hatte
das Wort schlicht nur an *einer* der Stellen gemeldet, und ersetzt wird, was
gemeldet ist.

Die Engine kommt in diesen Tests nicht vor: die Funde werden von Hand gebaut.
Das ist Absicht — geprueft wird die Uebertragungsregel, nicht das Sprachmodell,
und ein Test, der das spaCy-Modell laedt, waere langsam und wackelig.

``test_word_dokument_beide_stellen_werden_ersetzt`` am Ende schliesst den Kreis
und weist das urspruengliche Fehlerbild am echten DOCX nach.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import List

from docx import Document

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters.base import Segment  # noqa: E402
from backend.adapters.docx_adapter import DocxAdapter  # noqa: E402
from backend.pipeline import (  # noqa: E402
    Finding,
    build_replacements,
    propagate_findings,
)

# ── Hilfen ─────────────────────────────────────────────────────────────────


def segment(id_: str, text: str) -> Segment:
    return Segment(id=id_, text=text, location=None)


def fund(
    segment_id: str,
    text: str,
    segmenttext: str,
    entity_type: str = "PERSON",
    score: float = 0.85,
) -> Finding:
    """Ein Fund auf dem ersten Vorkommen von ``text`` in ``segmenttext``."""
    start = segmenttext.index(text)
    return Finding(
        segment_id=segment_id,
        entity_type=entity_type,
        start=start,
        end=start + len(text),
        score=score,
        text=text,
    )


def texte(befunde: List[Finding]) -> List[tuple]:
    """Vergleichbare Kurzform: (Segment, Start, Ende, Text, uebertragen)."""
    return [(f.segment_id, f.start, f.end, f.text, f.propagated) for f in befunde]


# ── Der Kern: ein Fund gilt im ganzen Dokument ─────────────────────────────


def test_fund_wird_in_anderes_segment_uebertragen():
    """Das Fehlerbild aus dem Betrieb, auf das Wesentliche eingedampft."""
    a = segment("p0", "Der Vertrag mit LogObject laeuft weiter.")
    b = segment("p1", "Im Zusammenspiel mit LogObject gilt ein Releasetakt.")

    ergebnis = propagate_findings([a, b], [fund("p0", "LogObject", a.text)])

    assert len(ergebnis) == 2
    nachgetragen = [f for f in ergebnis if f.propagated]
    assert len(nachgetragen) == 1
    assert nachgetragen[0].segment_id == "p1"
    assert b.text[nachgetragen[0].start : nachgetragen[0].end] == "LogObject"


def test_zweites_vorkommen_im_selben_segment():
    """Zwei Vorkommen in einem Absatz — das Modell meldet oft nur eines."""
    s = segment("p0", "LogObject liefert, und LogObject wartet die Anlage.")

    ergebnis = propagate_findings([s], [fund("p0", "LogObject", s.text)])

    assert [f.start for f in ergebnis] == [0, 23]
    assert [f.propagated for f in ergebnis] == [False, True]


def test_uebernommener_fund_traegt_typ_und_score_der_quelle():
    a = segment("p0", "Die ARGE ABI vergibt den Auftrag.")
    b = segment("p1", "Bericht der ARGE ABI vom Vortag.")
    quelle = fund("p0", "ARGE ABI", a.text, entity_type="ORGANIZATION", score=1.0)

    nachgetragen = [f for f in propagate_findings([a, b], [quelle]) if f.propagated]

    assert nachgetragen[0].entity_type == "ORGANIZATION"
    assert nachgetragen[0].score == 1.0
    assert nachgetragen[0].confidence == "gruen"


# ── Die Grenzen ────────────────────────────────────────────────────────────


def test_wortgrenze_wird_eingehalten():
    """``Meier`` darf nicht in ``Meiersberg`` oder ``Obermeier`` treffen."""
    a = segment("p0", "Andreas Meier war anwesend.")
    b = segment("p1", "Die Gemeinde Meiersberg meldet, Frau Obermeier auch.")

    ergebnis = propagate_findings([a, b], [fund("p0", "Meier", a.text)])

    assert not any(f.propagated for f in ergebnis)


def test_grossschreibung_zaehlt():
    """Uebertragen werden Eigennamen — ``sand`` ist nicht ``Sand``."""
    a = segment("p0", "Herr Sand unterschreibt.")
    b = segment("p1", "Der Boden besteht aus sand und Kies.")

    ergebnis = propagate_findings([a, b], [fund("p0", "Sand", a.text)])

    assert not any(f.propagated for f in ergebnis)


def test_bestehender_fund_wird_nicht_angeschnitten():
    """Ein Nachtrag darf nie in eine schon belegte Stelle hineinragen.

    Sonst entstuenden ueberlappende Ersetzungen, und ``build_replacements``
    braeche den ganzen Lauf mit einem ``ValueError`` ab.
    """
    a = segment("p0", "Andreas Meier und Beat Vogel.")
    b = segment("p1", "Meier allein.")

    ergebnis = propagate_findings(
        [a, b],
        # ``Meier`` ist anderswo ein eigener Fund und damit ein Kandidat fuer
        # den Nachtrag — in ``p0`` steckt er aber im belegten ``Andreas Meier``.
        [fund("p0", "Andreas Meier", a.text), fund("p1", "Meier", b.text)],
    )

    assert [f.text for f in ergebnis if f.segment_id == "p0"] == ["Andreas Meier"]
    stellen = sorted((f.start, f.end) for f in ergebnis if f.segment_id == "p0")
    for (a1, e1), (a2, e2) in zip(stellen, stellen[1:]):
        assert e1 <= a2, f"Ueberlapp {a1}-{e1} / {a2}-{e2}"


def test_laengster_begriff_belegt_zuerst():
    """``Andreas Meier`` muss vor ``Meier`` zum Zug kommen."""
    a = segment("p0", "Andreas Meier eroeffnet.")
    b = segment("p1", "Nur Meier zeichnet.")
    c = segment("p2", "Andreas Meier zeichnet mit.")

    ergebnis = propagate_findings(
        [a, b, c], [fund("p0", "Andreas Meier", a.text), fund("p1", "Meier", b.text)]
    )

    in_c = [f for f in ergebnis if f.segment_id == "p2"]
    assert len(in_c) == 1
    assert in_c[0].text == "Andreas Meier"


def test_nur_ner_typen_werden_uebertragen():
    """Muster-Erkenner finden ihren Text ohnehin ueberall — kein Nachtrag."""
    a = segment("p0", "Mail an a.muster@example.invalid bitte.")
    b = segment("p1", "Kopie an a.muster@example.invalid senden.")
    quelle = fund(
        "p0", "a.muster@example.invalid", a.text, entity_type="EMAIL_ADDRESS", score=1.0
    )

    ergebnis = propagate_findings([a, b], [quelle])

    assert not any(f.propagated for f in ergebnis)


def test_zu_kurze_begriffe_werden_nicht_uebertragen():
    """Unter drei Zeichen waere ein Fehlalarm zu teuer."""
    a = segment("p0", "AG traegt die Kosten.")
    b = segment("p1", "Die AG und die AG.")

    ergebnis = propagate_findings([a, b], [fund("p0", "AG", a.text)])

    assert not any(f.propagated for f in ergebnis)


def test_begriff_ohne_buchstaben_wird_nicht_uebertragen():
    a = segment("p0", "Position 4711 ist offen.")
    b = segment("p1", "Weitere 4711 Stueck, Rechnung 4711.")

    ergebnis = propagate_findings([a, b], [fund("p0", "4711", a.text)])

    assert not any(f.propagated for f in ergebnis)


def test_uneinheitlicher_typ_hoechster_score_gewinnt():
    """Derselbe Text, zwei Einordnungen — die sicherere gilt fuer den Nachtrag."""
    a = segment("p0", "Sitz in Zuerich.")
    b = segment("p1", "Die Zuerich versichert das Objekt.")
    c = segment("p2", "Zuerich meldet den Fall.")

    ergebnis = propagate_findings(
        [a, b, c],
        [
            fund("p0", "Zuerich", a.text, entity_type="LOCATION", score=0.85),
            fund("p1", "Zuerich", b.text, entity_type="ORGANIZATION", score=0.95),
        ],
    )

    nachgetragen = [f for f in ergebnis if f.propagated]
    assert [(f.segment_id, f.entity_type) for f in nachgetragen] == [
        ("p2", "ORGANIZATION")
    ]
    assert nachgetragen[0].score == 0.95


def test_bestehender_fund_wird_nicht_umetikettiert():
    """Ein Nachtrag darf eine erkannte Stelle nie bloss umbenennen.

    ``Sandra`` wird von der Engine mal als Person, mal als Ort gemeldet. Ohne
    diese Grenze wuerde der haeufigere Typ die korrekt erkannten Stellen des
    anderen ueberschreiben — die Erkennung waere danach *anders*, nicht besser.
    """
    a = segment("p0", "Frau Sandra meldet sich.")
    b = segment("p1", "Sandra")
    c = segment("p2", "Notiz von Sandra.")

    ergebnis = propagate_findings(
        [a, b, c],
        [
            fund("p0", "Sandra", a.text, entity_type="PERSON"),
            fund("p1", "Sandra", b.text, entity_type="LOCATION"),
        ],
    )

    unveraendert = [f for f in ergebnis if not f.propagated]
    assert [f.entity_type for f in unveraendert] == ["PERSON", "LOCATION"]


def test_laengerer_nachtrag_verdraengt_erkanntes_teilstueck():
    """Der haeufigste Fall im Betrieb: das Modell sieht nur den Vornamen.

    Steht in der Stichwortzeile nur ``Sandra`` als Fund, wuerde ohne diese
    Aufloesung ``Zbinden`` im Dokument stehen bleiben — der Nachtrag haette
    den Fall nur halb geloest.
    """
    a = segment("p0", "Die Beschwerde von Sandra Zbinden ging heute ein.")
    b = segment("p1", "Sandra Zbinden")

    ergebnis = propagate_findings(
        [a, b],
        [
            fund("p0", "Sandra Zbinden", a.text),
            fund("p1", "Sandra", b.text, entity_type="LOCATION"),
        ],
    )

    in_b = [f for f in ergebnis if f.segment_id == "p1"]
    assert [(f.text, f.entity_type, f.propagated) for f in in_b] == [
        ("Sandra Zbinden", "PERSON", True)
    ]


# ── Zusicherungen fuer die nachgelagerten Schritte ─────────────────────────


def test_ergebnis_ist_nach_dokumentreihenfolge_sortiert():
    a = segment("p0", "Meldung von Andreas Meier.")
    b = segment("p1", "Andreas Meier bestaetigt.")

    ergebnis = propagate_findings([a, b], [fund("p1", "Andreas Meier", b.text)])

    assert [f.segment_id for f in ergebnis] == ["p0", "p1"]


def test_ist_deterministisch():
    """Wackelnde Spans erzeugten Karteileichen im Vault."""
    a = segment("p0", "Andreas Meier und Beat Vogel.")
    b = segment("p1", "Beat Vogel trifft Andreas Meier.")
    quellen = [
        fund("p0", "Andreas Meier", a.text),
        fund("p0", "Beat Vogel", a.text),
    ]

    erst = propagate_findings([a, b], quellen)
    zweit = propagate_findings([a, b], quellen)

    assert texte(erst) == texte(zweit)


def test_ohne_uebertragbare_funde_bleibt_die_liste_gleich():
    a = segment("p0", "Nichts zu holen.")

    assert propagate_findings([a], []) == []


def test_nachtraege_erzeugen_keine_ueberlappenden_ersetzungen():
    """Gegenprobe gegen ``build_replacements._assert_no_overlap``."""
    a = segment("p0", "Andreas Meier trifft Beat Vogel.")
    b = segment("p1", "Beat Vogel und Andreas Meier erneut.")
    ergebnis = propagate_findings(
        [a, b],
        [fund("p0", "Andreas Meier", a.text), fund("p0", "Beat Vogel", a.text)],
    )

    # vault=None: anonymer Modus, dabei wird die Session nie benutzt.
    per_segment, _ = build_replacements(None, None, ergebnis)

    assert set(per_segment) == {"p0", "p1"}
    assert len(per_segment["p1"]) == 2


# ── Der Kreis schliesst sich: dasselbe am echten Word-Dokument ─────────────


def test_word_dokument_beide_stellen_werden_ersetzt():
    """Das Fehlerbild aus dem Betrieb, Ende zu Ende am DOCX nachgestellt."""
    document = Document()
    document.add_paragraph("Auftragsverhaeltnisse mit LogObject bestehen weiter.")
    document.add_paragraph("Im Zusammenspiel mit LogObject gilt ein Releasetakt.")
    puffer = io.BytesIO()
    document.save(puffer)
    daten = puffer.getvalue()

    adapter = DocxAdapter()
    segmente = adapter.extract_segments(daten)
    # Das Modell meldet nur die erste Stelle — genau das war der Fall.
    erkannt = [fund(segmente[0].id, "LogObject", segmente[0].text)]

    alle = propagate_findings(segmente, erkannt)
    per_segment, _ = build_replacements(None, None, alle)
    ergebnis = adapter.apply_replacements(daten, per_segment)

    absaetze = [p.text for p in Document(io.BytesIO(ergebnis)).paragraphs]
    assert "LogObject" not in "\n".join(absaetze)
    assert absaetze[0].count("[[PERSON]]") == 1
    assert absaetze[1].count("[[PERSON]]") == 1
