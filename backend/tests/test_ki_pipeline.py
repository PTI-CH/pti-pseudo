"""Die KI-Schicht am Einhaengepunkt.

Der wichtigste Test ist ``test_ausfall_liefert_das_lokale_ergebnis``: Faellt
der Dienst aus, liefert der Lauf weiterhin das lokale Ergebnis. Ein Werkzeug,
das bei einer Stoerung beim Dienstleister stillsteht, waere ein Rueckschritt
gegenueber dem Zustand ohne KI-Schicht.

Kein Test hier spricht mit dem echten Dienst. Die KI wird hereingereicht —
sonst kostete jeder Testlauf Geld und haenge von einer fremden Verfuegbarkeit
ab.
"""

from __future__ import annotations

from backend import pipeline
from backend.adapters import Segment
from backend.ki.infomaniak import KiFehler

TEXT = "Die Auskunft erteilte Frau Sandra Zbinden am Montag."


class KiDoppel:
    """Liefert eine vorbereitete Antwort oder wirft."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = 0
        self.letzte_nachrichten = None

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        self.aufrufe += 1
        self.letzte_nachrichten = nachrichten
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort


def _segmente():
    return [Segment(id="text", text=TEXT, location=None)]


def test_ohne_anforderung_wird_nicht_gefragt():
    """Vorgabe aus: Wer nicht fragt, schickt nichts hinaus."""
    ki = KiDoppel({"funde": []})
    ergebnis = pipeline.detect_in_segments(_segmente(), ki=ki)
    assert ki.aufrufe == 0
    assert ergebnis.ki_geprueft is False


def test_mit_anforderung_wird_die_grenze_korrigiert():
    """Der Zielfall der ganzen Etappe."""
    ki = KiDoppel(
        {"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]}
    )
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 1
    assert ergebnis.ki_geprueft is True
    personen = [f.text for f in ergebnis.findings if f.entity_type == "PERSON"]
    assert "Sandra Zbinden" in personen


def test_ausfall_liefert_das_lokale_ergebnis():
    """Der wichtigste Fall.

    Die lokale Erkennung ist mit 95 % Namen-Recall der Normalbetrieb, kein
    Notbehelf. Bei einer Stoerung still stehenzubleiben waere schlechter als
    der Zustand vor dieser Etappe.
    """
    ki = KiDoppel(KiFehler("Dienst weg"))
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ergebnis.ki_geprueft is False
    assert ergebnis.findings, "die lokalen Funde muessen erhalten bleiben"


def test_ohne_fundkandidaten_kein_aufruf():
    """Nichts gefunden, nichts zu pruefen — und nichts geht hinaus."""
    ki = KiDoppel({"funde": []})
    leer = [Segment(id="text", text="Die Zahlen lauten 1, 2 und 3.", location=None)]
    ergebnis = pipeline.detect_in_segments(leer, ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 0
    assert ergebnis.ki_geprueft is False


def test_nur_fenster_gehen_hinaus():
    """Die Datenschutz-Zusage, am Einhaengepunkt geprueft.

    Der Segmenttext ist laenger als das Fenster; was ausserhalb liegt, darf
    die Anfrage nicht erreichen.
    """
    lang = "Streng vertraulicher Vorspann ohne Namen. " * 6 + TEXT
    ki = KiDoppel({"funde": []})
    pipeline.detect_in_segments(
        [Segment(id="text", text=lang, location=None)], ki_pruefung=True, ki=ki
    )
    inhalt = ki.letzte_nachrichten[-1]["content"]
    assert inhalt.count("vertraulicher") <= 1


def test_korrektur_wird_durchs_dokument_getragen():
    """Der Grund fuer die Reihenfolge KI vor propagate_findings.

    Wird `Sandra` zu `Sandra Zbinden` korrigiert, muss die Uebertragung den
    VOLLSTAENDIGEN Namen an die zweite Stelle tragen — nicht den halben.
    """
    zwei = [
        Segment(id="a", text="Die Auskunft erteilte Frau Sandra Zbinden.", location=None),
        Segment(id="b", text="Zustaendig: Sandra Zbinden", location=None),
    ]
    ki = KiDoppel(
        {"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]}
    )
    ergebnis = pipeline.detect_in_segments(zwei, ki_pruefung=True, ki=ki)
    treffer = [f for f in ergebnis.findings if f.text == "Sandra Zbinden"]
    assert {f.segment_id for f in treffer} >= {"a", "b"}


class KiFolge:
    """Liefert je Aufruf die naechste vorbereitete Antwort — oder wirft."""

    def __init__(self, antworten):
        self.antworten = list(antworten)
        self.aufrufe = 0

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        a = self.antworten[self.aufrufe]
        self.aufrufe += 1
        if isinstance(a, Exception):
            raise a
        return a


def _viele_namen(anzahl: int):
    """Ein Segment mit `anzahl` erkennbaren Personennamen."""
    text = ". ".join(f"Es schrieb Andrea Beispiel{i}" for i in range(anzahl)) + "."
    return [Segment(id="text", text=text, location=None)]


def test_ausfall_eines_stuecks_trifft_nur_dieses(monkeypatch):
    """Der Grund fuer die Stueckelung.

    Faellt eine Anfrage aus, bleiben nur ihre Fenster unkorrigiert — nicht das
    ganze Dokument. Das Ergebnis gilt dann aber NICHT als geprueft.
    """
    from backend.ki import namen

    monkeypatch.setattr(namen, "MAX_FENSTER_JE_ANFRAGE", 2)
    segmente = _viele_namen(4)
    lokal = pipeline.detect_in_segments(segmente)
    anzahl = len([f for f in lokal.findings if f.entity_type == "PERSON"])
    if anzahl < 3:
        import pytest

        pytest.skip(f"Das Modell fand nur {anzahl} Namen — Fall nicht herstellbar.")

    # Der Fehler ist bewusst NICHT wiederholbar: Sonst wuerde das Stueck ein
    # zweites Mal angefragt und der Test pruefte die Wiederholung mit statt
    # der Stueckelung.
    ki = KiFolge(
        [
            {"funde": []},
            KiFehler("HTTP 400", wiederholbar=False),
            {"funde": []},
            {"funde": []},
        ]
    )
    ergebnis = pipeline.detect_in_segments(segmente, ki_pruefung=True, ki=ki)
    assert ki.aufrufe >= 2, "es muss gestueckelt worden sein"
    assert ergebnis.ki_geprueft is False, (
        "ein teilweise geprueftes Ergebnis darf nicht als geprueft gelten"
    )
    assert ergebnis.findings, "die lokalen Funde bleiben erhalten"


def test_haengender_aufruf_wird_einmal_wiederholt():
    """Der Grund fuer die Wiederholung.

    Am 2026-08-24 antwortete der Dienst in vier von neun Faellen gar nicht.
    Ein zweiter Versuch bringt die Ausfallquote rechnerisch von 44 % auf 19 %.
    """
    ki = KiFolge([KiFehler("timed out"), {"funde": [
        {"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]}])
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 2, "der zweite Versuch muss stattfinden"
    assert ergebnis.ki_geprueft is True
    assert "Sandra Zbinden" in [f.text for f in ergebnis.findings]


def test_es_wird_nur_einmal_wiederholt():
    """Zwei Versuche, nicht drei. Jeder kostet die volle Zeitgrenze."""
    ki = KiFolge([KiFehler("timed out"), KiFehler("timed out"), {"funde": []}])
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 2
    assert ergebnis.ki_geprueft is False
    assert ergebnis.findings, "die lokalen Funde bleiben erhalten"


def test_klientfehler_wird_nicht_wiederholt():
    """Ein HTTP 400 aendert sich beim zweiten Mal nicht.

    Ihn zu wiederholen kostete nur einen zweiten vergeblichen Aufruf.
    """
    ki = KiFolge([KiFehler("HTTP 400", wiederholbar=False), {"funde": []}])
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 1, "kein zweiter Versuch bei einem Fehler unsererseits"
    assert ergebnis.ki_geprueft is False
