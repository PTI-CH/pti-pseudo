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
import pytest

from backend.adapters import Segment
from backend.ki import namen, zweitmeinung
from backend.ki.infomaniak import KiFehler

TEXT = "Die Auskunft erteilte Frau Sandra Zbinden am Montag."


class KiDoppel:
    """Liefert eine vorbereitete Antwort oder wirft."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = 0
        self.letzte_nachrichten = None
        #: Alle Anfragen, nicht nur die letzte. Seit die Messung zu Ort und
        #: Organisation mitlaeuft, ist die letzte Anfrage nicht mehr
        #: zwangslaeufig die Namenspruefung.
        self.alle_nachrichten = []
        self.schemata = []

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        self.aufrufe += 1
        self.letzte_nachrichten = nachrichten
        self.alle_nachrichten.append(nachrichten)
        self.schemata.append(schema)
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort

    @property
    def namensaufrufe(self) -> int:
        """Nur die Anfragen der Namenspruefung.

        `aufrufe` zaehlt alle KI-Schritte. Ein Test ueber die Namenspruefung
        soll nicht rot werden, weil daneben eine Stufe dazugekommen ist.
        """
        return sum(1 for s in self.schemata if s is namen.SCHEMA)


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
    assert ki.namensaufrufe == 1
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


def _leeres_segment():
    return [Segment(id="text", text="Die Zahlen lauten 1, 2 und 3.", location=None)]


def test_ohne_fundkandidaten_geht_nichts_hinaus():
    """Im Auslieferungszustand: nichts gefunden, nichts geht hinaus.

    Die Namenspruefung fragt nicht, weil es ohne Fundkandidaten kein Fenster
    gibt; die Zweitmeinung fragt nicht, weil ihr Schalter ab Werk aus steht.

    `ki_geprueft` ist trotzdem True. Frueher stand hier False mit der Lesart
    „nichts zu pruefen" — das zeigte dem Nutzer einen Hinweis, der nichts
    bedeutete. Ein Hinweis, der ohne Grund erscheint, bringt Menschen dazu,
    Hinweise zu uebergehen.
    """
    ki = KiDoppel({"funde": []})
    ergebnis = pipeline.detect_in_segments(
        _leeres_segment(), ki_pruefung=True, ki=ki
    )

    assert ki.aufrufe == 0
    assert ergebnis.ki_geprueft is True


def test_ohne_fundkandidaten_fragt_die_zugeschaltete_zweitmeinung(zweitmeinung_an):
    """Was Stufe D aendert, wenn man sie zuschaltet — ihre schaerfste Kante.

    Ein Dokument, in dem die lokale Erkennung nichts findet, ist genau der
    Fall, in dem eine Zweitmeinung etwas wert waere: Entweder ist wirklich
    nichts drin, oder alles wurde uebersehen, und ohne zu fragen laesst sich
    das nicht unterscheiden.

    Der Preis steht daneben: Dann geht auch ein Dokument hinaus, in dem gar
    keine Personendaten stehen. Wer den Schalter umlegt, entscheidet das mit.
    """
    ki = KiDoppel({"funde": []})
    pipeline.detect_in_segments(_leeres_segment(), ki_pruefung=True, ki=ki)

    assert ki.namensaufrufe == 0, "ohne Fundkandidaten gibt es kein Fenster"
    assert ki.aufrufe == 1, "die Zweitmeinung fragt trotzdem"


def test_nur_fenster_gehen_hinaus():
    """Die Datenschutz-Zusage, am Einhaengepunkt geprueft.

    Der Segmenttext ist laenger als das Fenster; was ausserhalb liegt, darf
    die Anfrage nicht erreichen — und zwar **jede** Anfrage. Seit die Messung
    zu Ort und Organisation mitlaeuft, gibt es davon mehrere: Das Modell haelt
    ``Streng`` im Vorspann sechsmal fuer einen Ort, und um jeden dieser
    Fehlalarme entsteht ebenfalls ein Fenster.

    Geprueft wird deshalb die Zusage selbst — kein Ausschnitt umfasst das
    Segment — statt einer Zahl, die nur fuer das eine Namensfenster stimmte.
    """
    lang = "Streng vertraulicher Vorspann ohne Namen. " * 6 + TEXT
    ki = KiDoppel({"funde": []})
    pipeline.detect_in_segments(
        [Segment(id="text", text=lang, location=None)], ki_pruefung=True, ki=ki
    )

    assert ki.alle_nachrichten, "es muss ueberhaupt gefragt worden sein"
    for nachrichten in ki.alle_nachrichten:
        inhalt = nachrichten[-1]["content"]
        assert len(inhalt) < len(lang), "kein Ausschnitt darf das Segment umfassen"

    # Die Namenspruefung ist die erste Anfrage; fuer sie gilt die urspruengliche,
    # schaerfere Zusicherung weiterhin.
    assert ki.alle_nachrichten[0][-1]["content"].count("vertraulicher") <= 1


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
    """Liefert der NAMENSPRUEFUNG je Aufruf die naechste vorbereitete Antwort.

    Andere Anfragen — die Messung zu Ort und Organisation, die Zweitmeinung —
    bekommen eine leere Antwort und zaehlen nicht mit. Ohne diese Trennung
    haenge jeder Test der Wiederholungslogik daran, wie viele KI-Schritte es
    gerade sonst noch gibt, und eine neue Stufe machte sie alle rot, ohne dass
    an der Wiederholung etwas kaputt waere.
    """

    def __init__(self, antworten):
        self.antworten = list(antworten)
        self.aufrufe = 0

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        if schema is not namen.SCHEMA:
            return {"funde": []}
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


# ── Ort und Organisation: nur messen ───────────────────────────────────────

TEXT_GEMISCHT = (
    "Frau Sandra Zbinden von der Kantonspolizei Bern meldete den Vorfall in Thun."
)


class KiNachSchema:
    """Antwortet je nach gestelltem Schema — wie der echte Dienst auch."""

    def __init__(self, person, eigenname):
        self.person = person
        self.eigenname = eigenname
        self.schemata = []

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        self.schemata.append(schema)
        gefragt = "ist_eigenname" in str(schema)
        antwort = self.eigenname if gefragt else self.person
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    @property
    def wurde_gemessen(self) -> bool:
        return any("ist_eigenname" in str(s) for s in self.schemata)


def _gemischt():
    return [Segment(id="text", text=TEXT_GEMISCHT, location=None)]


def _ohne_person(ergebnis):
    return sorted(
        (f.entity_type, f.text)
        for f in ergebnis.findings
        if f.entity_type != "PERSON"
    )


def test_messung_zu_ort_und_organisation_aendert_keinen_fund():
    """Der Waechter dieser Etappe.

    Die Messung fragt die KI zu Ort- und Organisationsfunden, **wendet aber
    nichts an**. Selbst wenn die KI jeden davon fuer ein Gattungswort haelt,
    muss die Fundliste danach Zeichen fuer Zeichen dieselbe sein wie ohne
    KI-Pruefung. Faellt dieser Test, ist aus einer Messung unbemerkt ein
    Eingriff geworden.
    """
    ki = KiNachSchema(
        person={"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]},
        eigenname={
            "funde": [
                {"nummer": 0, "ist_eigenname": False},
                {"nummer": 1, "ist_eigenname": False},
            ]
        },
    )
    ohne = pipeline.detect_in_segments(_gemischt())
    mit = pipeline.detect_in_segments(_gemischt(), ki_pruefung=True, ki=ki)

    assert ki.wurde_gemessen, "die Messfrage muss ueberhaupt gestellt worden sein"
    assert _ohne_person(mit) == _ohne_person(ohne)


def test_ausfall_der_messung_kostet_den_lauf_nicht():
    """Die Messung ist ein Nebenprodukt — sie darf keine Erkennung kosten.

    ``ki_geprueft`` bleibt ``True``: Die Aussage des Flags betrifft die
    angewandte Namenspruefung, und die ist vollstaendig durchgelaufen.
    """
    ki = KiNachSchema(
        person={"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]},
        eigenname=KiFehler("Dienst nicht erreichbar"),
    )
    ergebnis = pipeline.detect_in_segments(_gemischt(), ki_pruefung=True, ki=ki)

    personen = [f.text for f in ergebnis.findings if f.entity_type == "PERSON"]
    assert "Sandra Zbinden" in personen
    assert ergebnis.ki_geprueft is True


# ── Stufe D: Zweitmeinung auf dem maskierten Text ──────────────────────────

#: Hier uebersieht spaCy `Zbinden` VOLLSTAENDIG und findet nur `Stalder`
#: (am 2026-08-25 nachgemessen). Genau das ist der blinde Fleck der
#: Fensterloesung: Ohne lokalen Fund entsteht kein Fenster, und die
#: Namenspruefung sieht die Stelle nie.
TEXT_LUECKE = "Die Meldung erfolgte durch Zbinden; gegengezeichnet von Stalder."


class KiStufeD:
    """Antwortet je nach Schema — Namenspruefung, Messung, Zweitmeinung."""

    def __init__(self, person=None, zweitmeinung=None):
        self.person = person or {"funde": []}
        self.zweit = zweitmeinung or {"funde": []}
        self.schemata = []
        self.anfragen = []

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        # Unterschieden wird ueber die IDENTITAET des Schemas, nicht ueber
        # dessen Text: "typ" steckt als Teilwort in "type" und traefe damit
        # jedes Schema.
        self.schemata.append(schema)
        self.anfragen.append(nachrichten[-1]["content"])
        if schema is zweitmeinung.SCHEMA:
            antwort = self.zweit
        elif schema is namen.SCHEMA_EIGENNAME:
            antwort = {"funde": []}
        else:
            antwort = self.person
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    @property
    def zweitmeinung_gefragt(self) -> bool:
        return any(s is zweitmeinung.SCHEMA for s in self.schemata)


def _luecke():
    return [Segment(id="text", text=TEXT_LUECKE, location=None)]


@pytest.fixture
def zweitmeinung_an(monkeypatch):
    """Schaltet den Betriebsschalter fuer die Dauer eines Tests ein.

    Die Vorgabe ist **aus** (siehe `backend/config.py`): Die Stufe verdreifacht
    die Anfragen je Dokument und liess dadurch am echten Dienst die
    Namenspruefung ausfallen. Wer sie hier prueft, schaltet sie ausdruecklich
    ein — so bleibt im Test sichtbar, dass sie im Betrieb nicht laeuft.
    """
    from backend.config import get_settings

    monkeypatch.setenv("KI_ZWEITMEINUNG", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_zweitmeinung_ergaenzt_einen_uebersehenen_namen(zweitmeinung_an):
    """Der Zielfall von Stufe D — A's blinder Fleck wird geschlossen."""
    ki = KiStufeD(
        zweitmeinung={"funde": [{"text": "Zbinden", "typ": "PERSON"}]}
    )
    ohne = pipeline.detect_in_segments(_luecke())
    mit = pipeline.detect_in_segments(_luecke(), ki_pruefung=True, ki=ki)

    assert ki.zweitmeinung_gefragt
    assert "Zbinden" not in [f.text for f in ohne.findings]
    zusatz = [f for f in mit.findings if f.text == "Zbinden"]
    assert zusatz, "der uebersehene Name muss ergaenzt worden sein"
    assert zusatz[0].confidence == "gelb"


def test_zweitmeinung_geht_nur_maskiert_hinaus(zweitmeinung_an):
    """Die Zusage dieser Stufe, am Einhaengepunkt geprueft.

    `Stalder` ist erkannt und geht deshalb maskiert hinaus. `Zbinden` ist
    uebersehen und steht im Klartext in der Anfrage — das ist kein Leck,
    sondern der ganze Zweck: Was hier hinausgeht, ist genau das, was die
    lokale Erkennung nicht gefunden hat.
    """
    ki = KiStufeD()
    pipeline.detect_in_segments(_luecke(), ki_pruefung=True, ki=ki)

    (anfrage,) = [
        inhalt
        for schema, inhalt in zip(ki.schemata, ki.anfragen)
        if schema is zweitmeinung.SCHEMA
    ]
    assert "Stalder" not in anfrage, "der erkannte Name ist maskiert"
    assert "Zbinden" in anfrage, "der uebersehene ist genau der Zweck"


def test_ausfall_der_zweitmeinung_kostet_den_lauf_nicht(zweitmeinung_an):
    """Kein Ausfall darf eine Erkennung kosten — aber er wird angezeigt."""
    ki = KiStufeD(zweitmeinung=KiFehler("Dienst nicht erreichbar"))
    ergebnis = pipeline.detect_in_segments(_luecke(), ki_pruefung=True, ki=ki)

    assert [f.text for f in ergebnis.findings], "das lokale Ergebnis bleibt"
    assert ergebnis.ki_geprueft is False, "der Mensch muss das wissen"


def test_zweitmeinung_laesst_sich_abschalten(monkeypatch):
    """Der Betriebsschalter — und die Vorgabe ist **aus**.

    Geprueft wird beides: dass `false` die Stufe stumm schaltet, und dass die
    Vorgabe ohne gesetzte Variable ebenfalls aus ist. Letzteres ist die
    eigentliche Zusicherung — ein Deploy darf nicht stillschweigend anfangen,
    ganze Dokumente hinauszuschicken.
    """
    from backend.config import get_settings

    monkeypatch.setenv("KI_ZWEITMEINUNG", "false")
    get_settings.cache_clear()
    try:
        ki = KiStufeD(
            zweitmeinung={"funde": [{"text": "Zbinden", "typ": "PERSON"}]}
        )
        ergebnis = pipeline.detect_in_segments(_luecke(), ki_pruefung=True, ki=ki)
        assert not ki.zweitmeinung_gefragt, "kein Text darf hinausgegangen sein"
        assert "Zbinden" not in [f.text for f in ergebnis.findings]

        # Und ohne gesetzte Variable ebenso.
        monkeypatch.delenv("KI_ZWEITMEINUNG", raising=False)
        get_settings.cache_clear()
        assert get_settings().ki_zweitmeinung is False
    finally:
        get_settings.cache_clear()


def test_unerwartete_ausnahme_kostet_keinen_lauf(monkeypatch):
    """Die Zusage der ganzen Schicht, an ihrer schwaechsten Stelle geprueft.

    `_ki_pruefen` faengt jeden `KiFehler`. Am 2026-08-25 riss aber ein
    `AttributeError` aus der Auswertung einer missgestalteten Antwort den
    ganzen Lauf — kein KiFehler, also ungefangen. Dass die lokale Erkennung
    ueberlebt, darf nicht davon abhaengen, dass jede kuenftige Aenderung an
    jede Ausnahmeart gedacht hat.
    """

    def platzt(*args, **kw):
        raise RuntimeError("etwas ganz Unerwartetes")

    monkeypatch.setattr(pipeline, "_ki_pruefen", platzt)
    monkeypatch.setattr(pipeline, "_ki_zweitmeinung", platzt)

    ergebnis = pipeline.detect_in_segments(
        _segmente(), ki_pruefung=True, ki=KiDoppel({"funde": []})
    )
    assert [f.text for f in ergebnis.findings], "die lokalen Funde bleiben"
    assert ergebnis.ki_geprueft is False


# ── Was von einem Fundtext ins Knotenprotokoll darf ────────────────────────


def test_protokolltext_laesst_gattungswoerter_durch():
    """Die Messung braucht genau diese Klasse — sie muss lesbar bleiben.

    Faellt dieser Test, ist der Filter zu scharf geworden und die Messung
    misst nichts mehr: Im Protokoll stuenden nur noch Platzhalter, und die
    Frage, ob die KI ``Fallnummer`` von einem echten Ortsnamen trennt, waere
    nicht mehr beantwortbar.
    """
    for wort in ("Fallnummer", "Kontrollschild", "Intern", "Bern", "Talblick"):
        assert pipeline.protokolltext(wort) == wort


def test_protokolltext_haelt_adressen_aus_dem_protokoll():
    """Der eigentliche Waechter: eine Adresse darf nie im Klartext erscheinen.

    Ein ``LOCATION``-Fund kann eine Wohnadresse sein. Das Knotenprotokoll
    faellt nicht unter die Vault-Verschluesselung und wird von logrotate
    tagelang aufgehoben — ein Personendatum hat dort nichts verloren.
    """
    adresse = "Muenzgasse 4, 3011 Bern"
    ergebnis = pipeline.protokolltext(adresse)

    assert adresse not in ergebnis
    assert "Muenzgasse" not in ergebnis
    assert "3011" not in ergebnis
    assert ergebnis.startswith("<mehrteilig:")


def test_protokolltext_meldet_dass_gefiltert_wurde():
    """Ausgefiltertes verschwindet nicht spurlos.

    Ohne diese Spur saehe eine Auswertung nach lauter kurzen Gattungswoertern
    aus, und niemand wuesste, dass die mehrteiligen Funde fehlen — die
    Messung waere still verzerrt.
    """
    assert pipeline.protokolltext("A" * 80) == "<lang:80>"
    assert pipeline.protokolltext("Police cantonale") == "<mehrteilig:16>"


def test_messung_protokolliert_keine_adresse(caplog):
    """Die Verdrahtung, nicht nur der Helfer.

    Ein Test, der nur ``protokolltext`` prueft, bliebe gruen, waehrend die
    Aufrufstelle in ``_ki_messen_ort_organisation`` weiter ``gefragt.markiert``
    ungefiltert hineinreicht. Deshalb laeuft hier die echte Messung.
    """
    import logging

    adresse = "Muenzgasse 4, 3011 Bern"
    segmente = [
        Segment(id="text", text=f"Die Meldung kam von {adresse} her.", location=None)
    ]
    findings = [
        pipeline.Finding(
            entity_type="LOCATION",
            start=20,
            end=20 + len(adresse),
            score=0.85,
            text=adresse,
            segment_id="text",
        )
    ]

    class KiMessung:
        def chat_json(self, nachrichten, schema):
            return {"funde": [{"nummer": 0, "ist_eigenname": True}]}

    with caplog.at_level(logging.INFO, logger="pti"):
        pipeline._ki_messen_ort_organisation(segmente, findings, KiMessung())

    protokoll = "\n".join(r.getMessage() for r in caplog.records)
    assert "KI-Messung" in protokoll, "die Messung muss ueberhaupt gelaufen sein"
    assert "Muenzgasse" not in protokoll
    assert "3011" not in protokoll
