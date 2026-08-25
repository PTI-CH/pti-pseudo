"""Stufe D: Zweitmeinung auf dem bereits pseudonymisierten Text.

Die Namenspruefung in :mod:`backend.ki.namen` schaut nur dorthin, wo die
lokale Erkennung schon etwas gefunden hat. Ein Name, den spaCy **vollstaendig**
uebersieht, bleibt damit uebersehen — das ist der bekannte blinde Fleck der
Fensterloesung, und er laesst sich mit Fenstern nicht schliessen.

Dieser Baustein dreht die Frage um. Er maskiert alle erkannten Funde und fragt
zum **Ergebnis**: „Stehen hier noch Personendaten?" Was gemeldet wird, hat die
lokale Erkennung uebersehen. Der Rueckweg ist dabei unerwartet einfach: Ein
uebersehener Name wurde ja gerade nicht ersetzt und steht im Original
wortgleich an derselben Stelle — eine Textsuche genuegt.

Was das kostet, und was nicht
------------------------------
Gemessen am Golden-Set (2026-08-24), Anteil des Dokumenttextes, der hinausgeht:

    Stufe A (Fenster)          66.5 %   erkannte Namen gehen mit
    Stufe B (ganze Segmente)   85.5 %   erkannte Namen gehen mit
    Stufe C (ganzes Dokument)  100 %    erkannte Namen gehen mit
    Stufe D (hier)             100 %    **kein erkannter Name geht mit**

Stufe D schlaegt C also in der entscheidenden Groesse. Der ehrliche Haken:
Pseudonymisieren entfernt Namen, nicht den Sachverhalt. Fallhergang, Adressen
von Gebaeuden, Tatbestaende gehen vollstaendig hinaus, und bei einem
Polizeidokument kann der Inhalt so heikel sein wie die Namen. Deshalb der
Betriebsschalter ``KI_ZWEITMEINUNG`` — abschaltbar ueber das Dashboard, ohne
Codeaenderung.

Zwei Regeln, die diese Datei tragen
------------------------------------
**Die Maske ist die Zusage.** Reisst sie, geht genau der Klartext hinaus, den
diese Stufe ersetzen soll. Deshalb wird von hinten nach vorn ersetzt (von
links verschoebe jede Ersetzung die folgenden Offsets) und deshalb pruefen
zwei Tests nichts anderes als das.

**Ein neuer Fund braucht einen Beleg.** Anders als :func:`namen.wende_an`, die
eine bestehende Grenze verschiebt, legt dieser Baustein NEUE Funde an. Ein
erfundener Name wuerde eine Stelle im Dokument ersetzen, die niemand geprueft
hat. Die Schranken in :func:`wende_an` sind entsprechend enger.
"""

from __future__ import annotations

import logging
from typing import Sequence

from backend.adapters import Segment
from backend.engine import entities as ents

logger = logging.getLogger("pti.ki.zweitmeinung")

__all__ = [
    "MIN_FUNDLAENGE",
    "SCHEMA",
    "SCORE",
    "SELTENHEITSRANG",
    "ZEICHENBUDGET",
    "baue_nachrichten",
    "ist_plausibler_name",
    "maskiere",
    "stueckle",
    "wende_an",
]

#: Zeichen je Anfrage.
#:
#: Stufe D bringt die Abschneide-Fehlerquelle zurueck, die die Fensterloesung
#: nicht hatte: Am 2026-08-24 lieferte Ministral-3 bei knappem Token-Budget
#: eine oeffnende Klammer und danach 2000 Zeichen Leerraum. Bei vierzig Namen
#: in **einer** Antwort geht so ein Ausfall still aus — Namen fehlen, ohne dass
#: es auffaellt.
#:
#: Gestueckelt trifft ein Ausfall ein Stueck statt das Dokument. Sechstausend
#: Zeichen sind rund zwei Schreibmaschinenseiten: gross genug, dass ein
#: uebliches Protokoll in ein bis zwei Anfragen passt, klein genug, dass die
#: Antwort nicht ans Budget stoesst.
ZEICHENBUDGET = 6000

#: Mindestlaenge eines gemeldeten Fundes.
#:
#: Dieselbe Schranke und derselbe Grund wie bei
#: ``pipeline.MIN_UEBERTRAGUNGSLAENGE``: Kuerzeres traegt zu wenig Eigenheit,
#: und weil ``propagate_findings`` **nach** dieser Stufe laeuft, vervielfachte
#: sich ein Fehlalarm auf einem Zweibuchstaber ueber das ganze Dokument.
MIN_FUNDLAENGE = 3

#: Score neuer Funde aus der Zweitmeinung — Ampel **gelb**, nicht gruen.
#:
#: Bewusst unter der Gruen-Schwelle von 0.85 (siehe ``Finding.confidence``).
#: Diese Funde hat keine lokale Ebene bestaetigt: kein Muster, keine
#: Pruefziffer, kein Begriffslisteneintrag, nicht einmal das NER-Modell. Sie
#: dem Menschen gruen vorzulegen behauptete eine Sicherheit, die es nicht gibt.
SCORE = 0.7

#: Antwortschema — eine **Liste** auf oberster Ebene, kein Objekt.
#:
#: Das ist am 2026-08-25 erarbeitet und nicht gewaehlt. Zuerst stand hier ein
#: Objekt mit dem Feld ``funde``, wie beim Namensschema. Ergebnis am echten
#: Dienst: Apertus antwortete trotzdem mit einer blossen Liste, und bei
#: laengeren Eingaben kippte es in eine Schleife aus Leerraum bis ans
#: Token-Limit — dasselbe Fehlerbild, das seinerzeit Ministral-3 zeigte.
#:
#: Der Grund lag in der Anweisung: Sie endete auf „gib eine leere Liste
#: zurueck" und kaempfte damit gegen das Objekt-Schema. **Infomaniaks
#: ``json_schema`` erzwingt die Form nicht** — es lenkt nur. Wo Anweisung und
#: Schema auseinanderlaufen, entscheidet die Anweisung, und im Zweifel
#: degeneriert das Modell.
#:
#: Gemessen, je drei Laeufe an kurzer, langer und fundfreier Eingabe:
#:
#:     Objekt-Schema, "leere Liste"      3/3 gueltig, aber falsche Form
#:     Objekt-Schema, ohne Schlusssatz   0/3
#:     Liste ohne Beispiel               9/9 gueltig, aber immer leer
#:     Liste MIT Beispiel                findet Zbinden und Wyttenbach, 1.3 s
SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "typ": {"type": "string", "enum": list(ents.NER_ENTITIES)},
        },
        "required": ["text", "typ"],
    },
}

#: Zwei Teile, beide am Dienst erarbeitet (2026-08-25):
#:
#: **Das Beispiel ist nicht Zierat.** Ohne es lieferte das Modell neunmal von
#: neun eine leere Liste — gueltig, schnell und wertlos.
#:
#: **Die Ausschlussliste ebenso wenig.** Mit Beispiel, aber ohne sie meldete
#: der erste Golden-Set-Lauf ``Frau``, ``Herr``, ``Leitung``, ``Protokoll``
#: und ``AHV`` als Namen und drueckte die Precision von 91.9 % auf 81.0 %.
#: Gemessen an denselben Faellen:
#:
#:     ohne Ausschluss:  ['AHV-Nummer', 'Sachbearbeitung', 'Person']
#:                       ['Zbinden', '[Person]']
#:     mit Ausschluss:   ['AHV']
#:                       ['Zbinden']
#:
#: „Im Zweifel nichts" steht bewusst am Schluss: Diese Stufe ergaenzt zu einer
#: Erkennung, die bereits 96 % Namen-Recall hat. Ihr Wert liegt im seltenen
#: Fund, nicht in der Menge.
_ANWEISUNG = (
    "In den folgenden deutschen Behoerdentexten wurden Personendaten bereits "
    "durch Platzhalter in eckigen Klammern ersetzt, etwa [Person] oder [Ort]. "
    "Die Ersetzung ist unvollstaendig. Finde JEDEN Namen einer Person, eines "
    "Ortes, einer Strasse oder einer Organisation, der noch im Klartext "
    "dasteht. Gib ihn buchstabengetreu wieder. Erfinde nichts und gib nichts "
    "aus den eckigen Klammern zurueck. Antworte mit einer Liste; jeder "
    "Eintrag hat 'text' und 'typ' (PERSON, LOCATION oder ORGANIZATION). "
    "Beispiel: Zum Satz '[Person] traf Meier am Rosenweg.' lautet die Antwort "
    '[{"text": "Meier", "typ": "PERSON"}, '
    '{"text": "Rosenweg", "typ": "LOCATION"}].'
    " KEINE Namen sind: Anreden und Titel (Herr, Frau, Madame, Dr.),"
    " Funktions- und Rollenbezeichnungen (Leitung, Protokoll, Sachbearbeitung,"
    " Stellvertretung, Anwesend), Bezeichnungen von Datenarten (AHV, IBAN,"
    " Fallnummer, Kontrollschild) und allgemeine Woerter. Gib ausschliesslich"
    " echte Eigennamen zurueck. Im Zweifel nichts."
)


#: Ab diesem Haeufigkeitsrang gilt ein Wort als selten genug fuer einen Namen.
#:
#: Am 2026-08-25 an den Fehlalarmen und echten Funden des Golden-Sets
#: gemessen (``spacy.vocab[wort].rank``, kleiner Rang = haeufigeres Wort):
#:
#:     Fehlalarme   Frau 252 · Herr 915 · Kantonspolizei 956 · Leitung 1995
#:                  Sachbearbeitung 3019 · Anwesend 7578 · Protokoll 8410
#:     echte Funde  Fabian 9521 · Oberholzer 10940 · Stalder 11081
#:                  Zbinden 12350 · Bolliger 18930 · Zuercher/Wyttenbach unbekannt
#:
#: Neuntausend trennt beide Gruppen. Die Schwelle ist an achtzehn Woertern
#: gemessen und damit **grob** — sie taugt fuer die Frage „geht dieser
#: Zusatzvorschlag durch", nicht als allgemeine Namensregel.
SELTENHEITSRANG = 9000

#: Anreden und Titel, die vor einem Namen stehen koennen.
#:
#: Sie werden abgestreift, nicht als Ablehnungsgrund benutzt: ``Frau Kuhn``
#: ist ein Name mit Anrede davor, ``Frau`` allein ein Fehlalarm.
ANREDEN = frozenset(
    {
        "herr", "herrn", "frau", "fraeulein", "fräulein",
        "madame", "monsieur", "madame", "signora", "signore",
        "dr", "dr.", "prof", "prof.", "med", "phil",
    }
)


def ist_plausibler_name(text: str) -> bool:
    """Prueft, ob ein Zusatzvorschlag ueberhaupt ein Eigenname sein kann.

    **Diese Stufe darf streng sein, und das ist der Kern.** Sie ergaenzt zu
    einer Erkennung, die bereits 96 % Namen-Recall hat: Ein verworfener
    Zusatzvorschlag kostet fast nichts, ein uebernommener Fehlalarm ersetzt
    dagegen ein harmloses Wort mitten im Dokument. Die Regel „nie verwerfen"
    aus :mod:`backend.ki.namen` schuetzt **bestehende** Funde; auf neue,
    von keiner lokalen Ebene bestaetigte Vorschlaege ist sie nicht anwendbar.

    Der erste vollstaendige Lauf gegen den Dienst (2026-08-25) meldete ohne
    diesen Filter ``Frau``, ``Herr``, ``Leitung``, ``Protokoll``, ``2026`` und
    ``2024`` als Namen und drueckte die Precision von 91.9 % auf 79.1 %. Eine
    Ausschlussliste in der Anweisung half in Einzelfaellen, ueber das ganze
    Golden-Set nicht — das Modell haelt sich schlicht nicht daran. Ein
    Filter im eigenen Haus dagegen haelt immer.

    **Was er kostet:** Ein haeufiges Wort kommt nicht durch, auch wenn es ein
    Name ist — ``Bern`` (Rang 956) oder ``Kantonspolizei`` (956) meldet diese
    Stufe nicht mehr nach. Das ist vertretbar, weil genau solche Namen dem
    NER-Modell gelaeufig sind und von der lokalen Erkennung kommen. Teuer
    waere es nur bei einem haeufigen Wort, das **zugleich** ein Name und lokal
    uebersehen ist; ein solcher Fall ist im Golden-Set nicht vorhanden.
    """
    from backend.engine.nlp import get_nlp_engine

    woerter = [w.strip(".,;:!?()[]") for w in text.split()]
    woerter = [w for w in woerter if w]
    # Anreden abstreifen, solange welche vorne stehen.
    while woerter and woerter[0].casefold() in ANREDEN:
        woerter.pop(0)

    if not woerter:
        return False
    if not any(z.isalpha() for w in woerter for z in w):
        return False
    if not woerter[0][:1].isupper():
        return False

    vocab = get_nlp_engine().nlp["de"].vocab
    return any(
        vocab[w].is_oov or vocab[w].rank >= SELTENHEITSRANG for w in woerter
    )


def maskiere(text: str, findings: Sequence) -> str:
    """Ersetzt jeden Fund durch sein deutsches Label in eckigen Klammern.

    **Von hinten nach vorn** — dieselbe Regel wie in
    ``pipeline.apply_replacements``: Von links begonnen verschoebe jede
    Ersetzung die Offsets aller folgenden Funde, und ab dem zweiten traefe die
    Maske daneben. Genau dort stuende dann ein Klarname in dem Text, der
    hinausgeht.

    Es entstehen bewusst **keine** echten Pseudonyme: Der maskierte Text ist
    ein Zwischenschritt, der weggeworfen wird. Ihn ueber
    ``pipeline.build_replacements`` zu bauen brauchte Sitzung und Vault und
    vergaebe dort Pseudonyme fuer etwas, das nie ein Mensch zu sehen bekommt.
    """
    for fund in sorted(findings, key=lambda f: f.start, reverse=True):
        marke = f"[{ents.get_label(fund.entity_type)}]"
        text = text[: fund.start] + marke + text[fund.end :]
    return text


def stueckle(
    segments: Sequence[Segment],
    nach_segment: dict,
    budget: int = ZEICHENBUDGET,
) -> list[list[tuple]]:
    """Maskiert alle Segmente und teilt sie auf Anfragen auf.

    ``nach_segment`` ordnet Segment-Id auf die Funde dieses Segments.

    Ein Segment wird **nie zerschnitten**, auch wenn es allein ueber dem
    Budget liegt: Ein Schnitt mitten im Text koennte einen Namen in zwei
    Haelften teilen, und beide Haelften waeren danach unauffindbar. Ein zu
    grosses Segment geht deshalb allein hinaus und nimmt das Budget in Kauf.

    Returns
    -------
    Liste von Stuecken; jedes Stueck eine Liste von ``(Segment, maskiert)``.
    """
    stuecke: list[list[tuple]] = []
    aktuell: list[tuple] = []
    laenge = 0

    for segment in segments:
        if not segment.text.strip():
            continue
        maskiert = maskiere(segment.text, nach_segment.get(segment.id, []))
        if aktuell and laenge + len(maskiert) > budget:
            stuecke.append(aktuell)
            aktuell, laenge = [], 0
        aktuell.append((segment, maskiert))
        laenge += len(maskiert)

    if aktuell:
        stuecke.append(aktuell)
    return stuecke


def baue_nachrichten(stueck: Sequence[tuple]) -> list[dict]:
    """Baut die Anfrage — **nur maskierter Text geht hinaus**."""
    zeilen = [maskiert for _, maskiert in stueck]
    return [
        {"role": "system", "content": _ANWEISUNG},
        {"role": "user", "content": "\n".join(zeilen)},
    ]


def _ist_platzhalter(text: str) -> bool:
    """Prueft, ob das Modell ein Masken-Label statt eines Fundes zurueckgibt.

    Der Fall ist nicht theoretisch: Steht ``Die Person Sandra Zbinden`` im
    Dokument, lautet der maskierte Text ``Die Person [Person]`` — und ``Person``
    kommt darin zweimal vor, einmal echt und einmal als Label. Ein Modell, das
    daraufhin ``Person`` meldet, faende im Original tatsaechlich eine Stelle.
    Ohne diese Schranke wuerde das Wort ersetzt.
    """
    ohne = text.strip().strip("[]").casefold()
    return any(
        ohne == ents.get_label(typ).casefold() for typ in ents.NER_ENTITIES
    )


def _eintraege(antwort) -> list:
    """Holt die Fundliste aus der Antwort — Liste **oder** Objekt.

    Das Schema verlangt eine Liste, und so antwortet der Dienst auch. Ein
    Modellwechsel oder eine Aenderung an der Anweisung kann das aber jederzeit
    in ein Objekt mit ``funde`` drehen; beides zu akzeptieren kostet zwei
    Zeilen und verhindert, dass eine inhaltlich richtige Antwort an der
    Verpackung scheitert.
    """
    if isinstance(antwort, list):
        return antwort
    if isinstance(antwort, dict):
        eintraege = antwort.get("funde")
        return eintraege if isinstance(eintraege, list) else []
    return []


def wende_an(
    stueck: Sequence[tuple],
    findings: Sequence,
    antwort: dict,
) -> list:
    """Legt neue Funde an — **nur belegte**.

    Dies ist der einzige Ort im Werkzeug, an dem ein Sprachmodell einen Fund
    *entstehen* laesst statt einen bestehenden zu verschieben. Ein erfundener
    Name ersetzte hier eine Stelle im Dokument, die keine lokale Ebene je
    gesehen hat. Ein Vorschlag wird deshalb nur uebernommen, wenn er **alle**
    Schranken passiert:

    * mindestens :data:`MIN_FUNDLAENGE` Zeichen,
    * Typ ist einer der drei NER-Typen (``entities.NER_ENTITIES``),
    * ist nicht selbst eines der Masken-Label (:func:`_ist_platzhalter`),
    * kommt **wortgleich im maskierten Text** vor — Beleg dafuer, dass das
      Modell den Text ueberhaupt gesehen hat,
    * kommt **wortgrenzengenau und gross-/kleinschreibungsempfindlich** im
      Originalsegment vor (dieselbe Schranke wie ``propagate_findings``),
    * ueberlappt dort keinen bestehenden Fund.

    Die Schranken greifen an unterschiedlichen Stellen, und das ist Absicht.
    Die Ueberlappung trifft den Fall, den sonst keine erwischt: Ein Wort, das
    **zweimal** vorkommt — einmal frei, einmal innerhalb eines erkannten
    Fundes. Im maskierten Text ist nur das freie sichtbar, die Textsuche im
    Original findet beide, und die zweite Stelle ist bereits abgedeckt.
    ``test_ueberlappung_wird_uebersprungen_der_rest_nicht`` haelt genau das
    fest.

    Returns
    -------
    Die Fundliste mit den uebernommenen Vorschlaegen — die uebergebene bleibt
    unveraendert.
    """
    from backend.pipeline import Finding, wortgrenzen_muster

    neu = list(findings)
    uebernommen = 0
    verworfen = 0

    for eintrag in _eintraege(antwort):
        text = (eintrag.get("text") or "").strip()
        typ = eintrag.get("typ")

        if (
            len(text) < MIN_FUNDLAENGE
            or typ not in ents.NER_ENTITIES
            or _ist_platzhalter(text)
            or not ist_plausibler_name(text)
            or not any(text in maskiert for _, maskiert in stueck)
        ):
            verworfen += 1
            continue

        muster = wortgrenzen_muster(text)
        treffer = 0
        for segment, _ in stueck:
            belegt = [(f.start, f.end) for f in neu if f.segment_id == segment.id]
            for m in muster.finditer(segment.text):
                if any(m.start() < e and m.end() > a for a, e in belegt):
                    continue
                neu.append(
                    Finding(segment.id, typ, m.start(), m.end(), SCORE, text)
                )
                treffer += 1

        if treffer:
            uebernommen += 1
        else:
            verworfen += 1

    if uebernommen or verworfen:
        logger.info(
            "Zweitmeinung: %d Vorschlaege uebernommen, %d verworfen",
            uebernommen,
            verworfen,
        )
    return neu
