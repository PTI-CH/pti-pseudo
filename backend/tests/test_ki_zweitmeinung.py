"""Tests der Zweitmeinung auf dem bereits pseudonymisierten Text (Stufe D).

Der Sinn dieser Stufe ist eine Umkehrung: Statt mehr Rohtext hinauszugeben,
geht der **maskierte** Text hinaus. Kein bereits erkannter Name verlaesst damit
das Haus — was das Modell meldet, hat die lokale Erkennung uebersehen.

Zwei Dinge werden hier geprueft, und beide sind Zusicherungen:

1. **Die Maske laesst keinen erkannten Fund durch.** Reisst das, geht genau
   der Klartext hinaus, den diese Stufe ersetzen sollte.
2. **Ein gemeldeter Fund wird nur uebernommen, wenn er belegt ist.** Anders als
   bei der Namenspruefung entstehen hier NEUE Funde; ein erfundener Name wuerde
   eine Stelle im Dokument ersetzen, die niemand geprueft hat.
"""

from __future__ import annotations

from backend.adapters import Segment
from backend.ki import zweitmeinung
from backend.pipeline import Finding


def _segment(text: str, sid: str = "s1") -> Segment:
    return Segment(id=sid, text=text, location=None)


def _fund(text: str, wort: str, typ: str = "PERSON", sid: str = "s1") -> Finding:
    start = text.index(wort)
    return Finding(sid, typ, start, start + len(wort), 0.85, wort)


# ── Maskieren ──────────────────────────────────────────────────────────────


def test_maske_ersetzt_jeden_fund():
    """Die Datenschutz-Zusage dieser Stufe: kein erkannter Fund geht hinaus."""
    text = "Frau Sandra Zbinden von der Kantonspolizei Bern meldete es."
    findings = [
        _fund(text, "Sandra Zbinden"),
        _fund(text, "Kantonspolizei Bern", "ORGANIZATION"),
    ]
    maskiert = zweitmeinung.maskiere(text, findings)

    assert "Sandra" not in maskiert
    assert "Zbinden" not in maskiert
    assert "Kantonspolizei" not in maskiert
    assert "meldete es." in maskiert, "der uebrige Text bleibt unangetastet"


def test_maske_arbeitet_von_hinten():
    """Von links wuerde jede Ersetzung die folgenden Offsets verschieben."""
    text = "Anna und Bertram und Cornelia."
    findings = [
        _fund(text, "Anna"),
        _fund(text, "Bertram"),
        _fund(text, "Cornelia"),
    ]
    maskiert = zweitmeinung.maskiere(text, findings)

    assert "Anna" not in maskiert
    assert "Bertram" not in maskiert
    assert "Cornelia" not in maskiert
    assert maskiert.count("[") == 3


def test_maske_ohne_funde_laesst_den_text_in_ruhe():
    text = "Hier steht nichts Schuetzenswertes."
    assert zweitmeinung.maskiere(text, []) == text


# ── Stueckeln ──────────────────────────────────────────────────────────────


def test_stueckle_haelt_das_zeichenbudget_ein():
    segmente = [_segment("x" * 100, f"s{i}") for i in range(10)]
    stuecke = zweitmeinung.stueckle(segmente, {}, budget=250)

    assert len(stuecke) >= 4
    for stueck in stuecke:
        assert sum(len(t) for _, t in stueck) <= 250 or len(stueck) == 1


def test_stueckle_zerschneidet_kein_segment():
    """Ein zerschnittenes Segment koennte einen Namen in zwei Haelften teilen."""
    segmente = [_segment("y" * 9000, "gross")]
    stuecke = zweitmeinung.stueckle(segmente, {}, budget=100)

    assert len(stuecke) == 1
    assert stuecke[0][0][1] == "y" * 9000


def test_stueckle_laesst_leere_segmente_aus():
    segmente = [_segment("   ", "leer"), _segment("Inhalt", "voll")]
    stuecke = zweitmeinung.stueckle(segmente, {})

    ids = [s.id for stueck in stuecke for s, _ in stueck]
    assert ids == ["voll"]


# ── Anfrage ────────────────────────────────────────────────────────────────


def test_nachrichten_enthalten_den_maskierten_text():
    text = "Frau Sandra Zbinden und Herr Kunz."
    segmente = [_segment(text)]
    nach_segment = {"s1": [_fund(text, "Sandra Zbinden")]}
    (stueck,) = zweitmeinung.stueckle(segmente, nach_segment)
    nachrichten = zweitmeinung.baue_nachrichten(stueck)

    inhalt = nachrichten[-1]["content"]
    assert "Sandra" not in inhalt, "der erkannte Name darf nicht hinausgehen"
    assert "Kunz" in inhalt, "der uebersehene Name ist genau der Zweck"


# ── Die Antwort anwenden: neue Funde brauchen einen Beleg ──────────────────

TEXT_LUECKE = "Frau Sandra Zbinden und Herr Ruedi Bolliger waren anwesend."


def _stueck_mit_luecke():
    """Sandra Zbinden ist erkannt, Ruedi Bolliger nicht — der Zielfall."""
    segmente = [_segment(TEXT_LUECKE)]
    nach_segment = {"s1": [_fund(TEXT_LUECKE, "Sandra Zbinden")]}
    (stueck,) = zweitmeinung.stueckle(segmente, nach_segment)
    return stueck, list(nach_segment["s1"])


def _neue(vorher, nachher):
    return [f for f in nachher if f not in vorher]


def test_gemeldeter_name_wird_als_gelber_fund_angelegt():
    """Der Zielfall: Die KI sieht, was spaCy uebersehen hat."""
    stueck, findings = _stueck_mit_luecke()
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Ruedi Bolliger", "typ": "PERSON"}]}
    )

    (zusatz,) = _neue(findings, neu)
    assert zusatz.text == "Ruedi Bolliger"
    assert TEXT_LUECKE[zusatz.start : zusatz.end] == "Ruedi Bolliger"
    assert zusatz.entity_type == "PERSON"
    assert zusatz.confidence == "gelb", "keine lokale Ebene hat das bestaetigt"


def test_erfundener_name_wird_verworfen():
    """Das Modell darf nichts erfinden — sonst ersetzt es ungepruefte Stellen."""
    stueck, findings = _stueck_mit_luecke()
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Gustav Meier", "typ": "PERSON"}]}
    )
    assert _neue(findings, neu) == []


def test_platzhalter_wird_nicht_zurueckgemeldet():
    """Ein Label aus der Maske ist kein Fund, auch wenn es im Text vorkommt."""
    text = "Die Person Sandra Zbinden meldete den Vorfall."
    segmente = [_segment(text)]
    nach_segment = {"s1": [_fund(text, "Sandra Zbinden")]}
    (stueck,) = zweitmeinung.stueckle(segmente, nach_segment)
    findings = list(nach_segment["s1"])

    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Person", "typ": "PERSON"}]}
    )
    assert _neue(findings, neu) == []


def test_ueberlappender_fund_wird_verworfen():
    """Was schon erkannt ist, wird nicht ein zweites Mal angelegt."""
    stueck, findings = _stueck_mit_luecke()
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Sandra", "typ": "PERSON"}]}
    )
    assert _neue(findings, neu) == []


def test_zu_kurzer_fund_wird_verworfen():
    """Weil propagate_findings danach laeuft, vervielfachte sich ein Kuerzel."""
    text = "Der Fall PB betrifft Herrn Ruedi Bolliger."
    segmente = [_segment(text)]
    (stueck,) = zweitmeinung.stueckle(segmente, {})

    neu = zweitmeinung.wende_an(
        stueck, [], {"funde": [{"text": "PB", "typ": "PERSON"}]}
    )
    assert neu == []


def test_falsche_grossschreibung_wird_verworfen():
    """Gross-/kleinschreibungsempfindlich wie propagate_findings."""
    stueck, findings = _stueck_mit_luecke()
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "ruedi bolliger", "typ": "PERSON"}]}
    )
    assert _neue(findings, neu) == []


def test_unbekannter_typ_wird_verworfen():
    stueck, findings = _stueck_mit_luecke()
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Ruedi Bolliger", "typ": "CH_AHV"}]}
    )
    assert _neue(findings, neu) == []


def test_wortgrenze_wird_eingehalten():
    """Ein Treffer mitten in einem laengeren Wort ist keiner."""
    text = "Der Bollingerweg fuehrt zum See."
    segmente = [_segment(text)]
    (stueck,) = zweitmeinung.stueckle(segmente, {})

    neu = zweitmeinung.wende_an(
        stueck, [], {"funde": [{"text": "Bollinger", "typ": "PERSON"}]}
    )
    assert neu == []


def test_ueberlappung_wird_uebersprungen_der_rest_nicht():
    """Die Ueberlappungsschranke, an dem Fall, der sie wirklich trifft.

    ``Stalder`` steht zweimal: einmal frei, einmal als Teil des erkannten
    ``Stalder AG``. Im maskierten Text ist nur das erste sichtbar — meldet das
    Modell ``Stalder``, findet die Textsuche im Original aber BEIDE.
    Die zweite Stelle ist bereits abgedeckt und darf keinen zweiten Fund
    bekommen; die erste ist ein echter Nachtrag.

    Der urspruengliche Ueberlappungstest lief ueber eine andere Schranke ins
    Ziel und liess diese hier ungeprueft — aufgefallen bei der Gegenprobe.
    """
    text = "Stalder meldete es. Die Firma Stalder AG bestaetigte den Eingang."
    treffer = text.index("Stalder AG")
    findings = [
        Finding(
            "s1",
            "ORGANIZATION",
            treffer,
            treffer + len("Stalder AG"),
            0.85,
            "Stalder AG",
        )
    ]
    (stueck,) = zweitmeinung.stueckle([_segment(text)], {"s1": findings})
    neu = zweitmeinung.wende_an(
        stueck, findings, {"funde": [{"text": "Stalder", "typ": "PERSON"}]}
    )

    (zusatz,) = _neue(findings, neu)
    assert zusatz.start == 0, "nur das freistehende Stalder wird nachgetragen"
    assert text[zusatz.start : zusatz.end] == "Stalder"


# ── Der Plausibilitaetsfilter fuer Zusatzvorschlaege ───────────────────────


def test_filter_verwirft_anreden_zahlen_und_gattungswoerter():
    """Am 2026-08-25 am echten Golden-Set gemessen — alle echten Fehler.

    Diese Stufe darf streng sein, und das ist der Kern: Sie **ergaenzt** zu
    einer Erkennung, die bereits 96 % Namen-Recall hat. Ein verworfener
    Zusatzvorschlag kostet fast nichts; ein uebernommener Fehlalarm dagegen
    ersetzt ein harmloses Wort im Dokument. Die Regel „nie verwerfen" schuetzt
    bestehende Funde — hier gilt sie nicht.
    """
    for wort in ("Frau", "Herr", "2026", "2024", "Leitung", "Protokoll",
                 "Sachbearbeitung", "Anwesend", "Kantonspolizei"):
        assert not zweitmeinung.ist_plausibler_name(wort), wort


def test_filter_behaelt_die_echten_namen():
    """Und dieselbe Messung von der anderen Seite."""
    for wort in ("Zbinden", "Oberholzer", "Fabian", "Zürcher", "Stalder",
                 "Wyttenbach", "Bolliger", "Ruedi Bolliger"):
        assert zweitmeinung.ist_plausibler_name(wort), wort


def test_filter_streift_die_anrede_ab():
    """`Frau Kuhn` ist ein Name mit Anrede davor, kein Fehlalarm."""
    assert zweitmeinung.ist_plausibler_name("Frau Kuhn")


def test_filter_verlangt_grossschreibung():
    assert not zweitmeinung.ist_plausibler_name("und")
