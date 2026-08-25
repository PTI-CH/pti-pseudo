"""Fachlogik der KI-Schicht: Fenster bauen, Antwort anwenden.

Dieser Baustein bestimmt, **wieviel Text das Haus verlaesst**. Er ist deshalb
von :mod:`backend.ki.infomaniak` getrennt: Ein Wechsel der Ausbaustufe —
Fenster gegen ganze Segmente gegen ganzes Dokument — beruehrt nur diese Datei.
Client, Schema und Fehlerbehandlung bleiben unberuehrt.

Zwei Regeln tragen die Datei, beide aus der Spec:

**Nur Fenster.** Je Fundkandidat gehen rund 20 Woerter hinaus, nie das Segment
und nie das Dokument. Preis: Ein Name, den spaCy vollstaendig uebersieht,
bleibt uebersehen — die KI sieht nur, wo die lokale Erkennung schon etwas
gefunden hat.

**Nie entfernen, und nur gefahrlos verkleinern.** Die Fehler sind
asymmetrisch: Ein Fehlalarm kostet ein Wort, das ein Mensch in der Fundliste
abwaehlt; ein zu Unrecht verworfener Fund kostet einen Klarnamen in einem
Dokument, das aussieht wie pseudonymisiert. Dieselbe Ueberlegung traegt seit
PR #8 ``pipeline.propagate_findings``.

Erweitern ist deshalb immer erlaubt, Entfernen (``ist_person: false``) nie.
Verkleinern ist erlaubt, **solange kein grossgeschriebenes Wort wegfaellt** —
denn nur ein solches koennte ein Namensteil sein, der danach im Klartext
stuende. Am 2026-08-24 am Dienst gemessen: spaCy meldete
``Sylvie Marchand a rédigé le procès-verbal`` als eine Person; die KI
antwortete ``Sylvie Marchand``. Diese Kuerzung deckt den Namen weiterhin
vollstaendig ab und laesst nur Fliesstext fallen. Sie zu verbieten kostete
Genauigkeit ohne Sicherheitsgewinn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Sequence

from backend.adapters import Segment

logger = logging.getLogger("pti.ki.namen")

__all__ = [
    "Fenster",
    "MAX_FENSTER_JE_ANFRAGE",
    "SCHEMA",
    "SCHEMA_EIGENNAME",
    "WORTRADIUS",
    "baue_eigenname_nachrichten",
    "baue_fenster",
    "baue_nachrichten",
    "stueckle",
    "wende_an",
]

#: Wieviele Fenster hoechstens in **eine** Anfrage gehen.
#:
#: Am 2026-08-24 mit Apertus gemessen: 1 Fenster 0.7 s, 2 Fenster rund
#: 1.1 s, 6 Fenster 2.2 s. Die Grundlast dominiert deutlich; die
#: Fensterzahl schlaegt nur schwach durch.
#:
#: **Warum nicht kleiner:** Jede Anfrage kostet die Grundlast erneut. Vierzig
#: Fenster in zwei Anfragen sind schneller als in vier — kleinere Stuecke sind
#: **langsamer**, nicht schneller. Das ist der Gegenteil dessen, was man
#: erwartet, und der Grund fuer die verhaeltnismaessig grosse Zahl hier.
#:
#: **Warum nicht groesser:** Faellt eine Anfrage aus, bleiben ihre Fenster
#: unkorrigiert. Zwanzig ist die Groesse, bei der ein Ausfall einen
#: ueberschaubaren Teil des Dokuments trifft statt das ganze.
MAX_FENSTER_JE_ANFRAGE = 20

#: Woerter links und rechts des Fundes.
#:
#: Zehn reichen fuer die Aufgabe „wo hoert der Name auf" und halten den
#: Ausschnitt klein genug, dass er kein Dokument rekonstruierbar macht.
WORTRADIUS = 10

_WORTGRENZE = re.compile(r"\S+")


@dataclass(frozen=True)
class Fenster:
    """Ein Textausschnitt um genau einen Fundkandidaten."""

    #: Fortlaufend ab 0. Die Antwort adressiert ausschliesslich darueber.
    nummer: int
    #: Index in der uebergebenen Fundliste — der Rueckweg zum ``Finding``.
    finding_index: int
    #: Der Ausschnitt. **Nur das verlaesst das Haus.**
    text: str
    #: Startversatz von ``text`` im Segmenttext.
    versatz: int
    #: Der markierte Fundtext selbst.
    markiert: str


def _wortgrenzen(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _WORTGRENZE.finditer(text)]


def baue_fenster(
    segments: Sequence[Segment],
    findings: Sequence,
    wortradius: int = WORTRADIUS,
    typen: Sequence[str] = ("PERSON",),
) -> list[Fenster]:
    """Baut je Fundkandidat ein Fenster.

    ``typen`` bestimmt, welche Funde ueberhaupt eines bekommen. Vorgabe ist
    ``PERSON`` — der angewandte Pfad. Die reine Messung zu Ort und
    Organisation (siehe ``pipeline._ki_messen_ort_organisation``) ruft mit
    ``("LOCATION", "ORGANIZATION")``.

    Uebertragene Funde bleiben immer aussen vor: Ein uebertragener Fund stammt
    aus einem anderen, bereits geprueften — ihn erneut zu pruefen kostet nur
    und schickte denselben Text ein zweites Mal hinaus.

    Das Fenster endet **immer** an der Segmentgrenze. Darueber hinaus gibt es
    keinen Kontext, der zu dieser Stelle gehoerte.
    """
    nach_id = {s.id: s for s in segments}
    fenster: list[Fenster] = []
    nummer = 0

    for index, fund in enumerate(findings):
        if fund.entity_type not in typen or getattr(fund, "propagated", False):
            continue
        segment = nach_id.get(fund.segment_id)
        if segment is None:
            continue

        worte = _wortgrenzen(segment.text)
        beruehrt = [
            i for i, (a, b) in enumerate(worte) if a < fund.end and b > fund.start
        ]
        if not beruehrt:
            continue

        von = max(0, beruehrt[0] - wortradius)
        bis = min(len(worte) - 1, beruehrt[-1] + wortradius)
        versatz = worte[von][0]
        ende = worte[bis][1]

        fenster.append(
            Fenster(
                nummer=nummer,
                finding_index=index,
                text=segment.text[versatz:ende],
                versatz=versatz,
                markiert=segment.text[fund.start : fund.end],
            )
        )
        nummer += 1

    return fenster


def stueckle(
    fenster: Sequence[Fenster], groesse: int | None = None
) -> list[list[Fenster]]:
    """Teilt die Fenster in Anfragen auf.

    ``groesse`` wird **zur Laufzeit** aus :data:`MAX_FENSTER_JE_ANFRAGE`
    gelesen, nicht als Vorgabewert gebunden. Sonst haette ein Vorgabewert die
    Konstante bei der Definition eingefroren und sie waere nicht mehr
    aenderbar — weder in einem Test noch spaeter ueber eine Einstellung.

    Ein Dokument mit vierzig Namen ergaebe sonst eine Anfrage von rund 57
    Sekunden — und faellt sie aus, bleibt das ganze Dokument unkorrigiert.
    Mit Stuecken trifft ein Ausfall nur einen Teil.

    Die Nummern bleiben dabei die der **gesamten** Liste: Sie sind der
    Rueckweg zum Fund, und ein Neudurchnummerieren je Stueck haette die
    Zuordnung zerrissen.
    """
    if groesse is None:
        groesse = MAX_FENSTER_JE_ANFRAGE
    if groesse < 1:
        raise ValueError("Stueckgroesse muss mindestens 1 sein.")
    return [list(fenster[i : i + groesse]) for i in range(0, len(fenster), groesse)]


#: Antwortschema. ``json_object`` ergaebe HTTP 400 — nur ``json_schema`` geht.
#:
#: **``additionalProperties: False`` ist hier kein Feinschliff, sondern der
#: Unterschied zwischen brauchbar und unbrauchbar.** Ohne die Zeile erfindet
#: das Modell Felder dazu — gemessen am 2026-08-24 an zwei Fenstern:
#:
#:     ohne:  247 Token, mit rolle, organisation, kontakt, zusammenfassung
#:     mit:    65 Token, genau die drei gefragten Felder
#:
#: Bei vielen langen Fenstern reisst damit das ``max_tokens``-Budget, die
#: Antwort wird abgeschnitten — und eine abgeschnittene Antwort ist kein
#: gueltiges JSON mehr.
#:
#: Es hat die Ausfaelle im ersten Golden-Set-Lauf allerdings **nicht**
#: behoben. Deren Ursache lag im Modell (siehe ``backend/config.py``); diese
#: Zeile spart Token und beugt einer zweiten, unabhaengigen Fehlerquelle vor.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "funde": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nummer": {"type": "integer"},
                    "name": {"type": "string"},
                    "ist_person": {"type": "boolean"},
                },
                "required": ["nummer", "name", "ist_person"],
            },
        }
    },
    "required": ["funde"],
}

_ANWEISUNG = (
    "Du pruefst markierte Textstellen in deutschsprachigen Behoerdendokumenten. "
    "Zu jedem nummerierten Ausschnitt ist eine Stelle mit >>> <<< markiert. "
    "Sage fuer jede Nummer: ist die Markierung Teil eines Personennamens, und "
    "wie lautet der VOLLSTAENDIGE Name genau so, wie er im Ausschnitt steht? "
    "Gib den Namen buchstabengetreu aus dem Ausschnitt wieder, erfinde nichts "
    "und ergaenze nichts. Titel und Anreden (Herr, Frau, Dr.) gehoeren nicht "
    "zum Namen."
)


#: Die Messfrage zu Ort und Organisation. **Bewusst getrennt von**
#: :data:`_ANWEISUNG`: Die Namensfrage wird angewandt und ist am Dienst
#: gemessen gut formuliert; sie um einen zweiten Zweck zu erweitern hiesse,
#: eine funktionierende Frage fuer eine Messung aufs Spiel zu setzen.
_ANWEISUNG_EIGENNAME = (
    "Du pruefst markierte Textstellen in deutschsprachigen Behoerdendokumenten. "
    "Zu jedem nummerierten Ausschnitt ist eine Stelle mit >>> <<< markiert. "
    "Sage fuer jede Nummer, ob die Markierung ein Eigenname ist — also der "
    "Name eines konkreten Ortes, einer Strasse, eines Gebaeudes oder einer "
    "Organisation. Ein allgemeines Wort wie 'Fallnummer', 'Kontrollschild', "
    "'Dienstgebrauch' oder 'Intern' ist KEIN Eigenname, auch wenn es "
    "grossgeschrieben ist."
)

#: Antwortschema der Messfrage. ``additionalProperties: False`` aus demselben
#: Grund wie bei :data:`SCHEMA`.
SCHEMA_EIGENNAME = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "funde": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nummer": {"type": "integer"},
                    "ist_eigenname": {"type": "boolean"},
                },
                "required": ["nummer", "ist_eigenname"],
            },
        }
    },
    "required": ["funde"],
}


def _nachrichten(fenster: Sequence[Fenster], anweisung: str) -> list[dict]:
    """Setzt Anweisung und markierte Fenstertexte zu einer Anfrage zusammen."""
    zeilen = []
    for f in fenster:
        markiert = f.text.replace(f.markiert, f">>>{f.markiert}<<<", 1)
        zeilen.append(f"{f.nummer}: {markiert}")
    return [
        {"role": "system", "content": anweisung},
        {"role": "user", "content": "\n".join(zeilen)},
    ]


def baue_nachrichten(fenster: Sequence[Fenster]) -> list[dict]:
    """Baut die Chat-Nachrichten — **nur die Fenstertexte gehen hinaus**."""
    return _nachrichten(fenster, _ANWEISUNG)


def baue_eigenname_nachrichten(fenster: Sequence[Fenster]) -> list[dict]:
    """Baut die Anfrage der Messung zu Ort und Organisation.

    Die Antwort darauf wird **nur protokolliert, nie angewandt** — siehe
    ``pipeline._ki_messen_ort_organisation``. Es gibt hier deshalb bewusst
    kein Gegenstueck zu :func:`wende_an`.
    """
    return _nachrichten(fenster, _ANWEISUNG_EIGENNAME)


def _nur_kleinschreibung(text: str) -> bool:
    """Steht im Text kein grossgeschriebenes Wort.

    Die Frage entscheidet, ob eine Verkleinerung harmlos ist. Ein Namensteil
    ist grossgeschrieben, Fliesstext nicht — mehr Unterscheidungskraft braucht
    es an dieser Stelle nicht, und mehr waere Wortartenraten (siehe
    ``backend/engine/wortarten.py``, wo genau das nicht traegt).
    """
    return not any(wort[:1].isupper() for wort in text.split())


def _darf_angewandt_werden(fenster: Fenster, alt, start: int, ende: int) -> bool:
    """Erweitert der Vorschlag, oder verkleinert er gefahrlos."""
    if (start, ende) == (alt.start, alt.end):
        return False
    if start <= alt.start and ende >= alt.end:
        return True
    if start >= alt.start and ende <= alt.end:
        versatz = fenster.versatz
        vorne = fenster.text[alt.start - versatz : start - versatz]
        hinten = fenster.text[ende - versatz : alt.end - versatz]
        return _nur_kleinschreibung(vorne) and _nur_kleinschreibung(hinten)
    # Vorne erweitern und hinten verkleinern (oder umgekehrt): dafuer gibt es
    # keinen belegten Fall, und ungeprueft gewaehrte Freiheit ist hier das
    # Risiko.
    return False


def wende_an(
    findings: Sequence,
    fenster: Sequence[Fenster],
    antwort: dict,
) -> tuple[list, int]:
    """Wendet die Vorschlaege an — erweiternd, oder gefahrlos verkleinernd.

    Ein Vorschlag wird verworfen, wenn er

    * eine unbekannte Fensternummer nennt,
    * einen leeren Namen liefert,
    * den Fund entfernen will (``ist_person: false``),
    * einen Namen liefert, der im Fenster gar nicht vorkommt (das Modell darf
      nichts erfinden),
    * gar nichts aendert,
    * die Grenze verkleinert und dabei ein grossgeschriebenes Wort fallen
      laesst (:func:`_darf_angewandt_werden`), oder
    * vorne erweitert und hinten verkleinert — oder umgekehrt.

    Returns
    -------
    (neue_findings, anzahl_verworfen)
        Die Zahl ist kein Schmuck: Aus ihr laesst sich ablesen, wie oft die KI
        etwas anderes wollte, als sie durfte — und damit, ob sich die naechste
        Ausbaustufe lohnt.
    """
    nach_nummer = {f.nummer: f for f in fenster}
    neu = list(findings)
    verworfen = 0

    for eintrag in antwort.get("funde", []):
        f = nach_nummer.get(eintrag.get("nummer"))
        name = (eintrag.get("name") or "").strip()

        if f is None or not name or eintrag.get("ist_person") is False:
            verworfen += 1
            continue

        stelle = f.text.find(name)
        if stelle < 0:
            verworfen += 1
            continue

        alt = neu[f.finding_index]
        start = f.versatz + stelle
        ende = start + len(name)
        if not _darf_angewandt_werden(f, alt, start, ende):
            verworfen += 1
            continue

        neu[f.finding_index] = replace(alt, start=start, end=ende, text=name)

    if verworfen:
        logger.info("KI-Vorschlaege verworfen: %d von %d", verworfen, len(fenster))
    return neu, verworfen
