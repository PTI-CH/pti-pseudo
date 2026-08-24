"""Tests der Begriffslisten-Pruefung.

Die Pruefung meldet Eintraege zum Loeschen. Der teurere Fehler ist deshalb der
**Fehlalarm**: Wer eine richtige Behoerde meldet, verleitet dazu, sie aus der
Liste zu entfernen — und dann steht ihr Name wieder im Klartext in jedem
Dokument. Die meisten Tests hier pruefen darum, was **nicht** gemeldet werden
darf.

Alle Beispiele sind echtes PTI-Fachvokabular aus dem Glossar bzw. dem am
2026-08-24 gemessenen Produktivbestand — keine Personendaten.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from begriffsliste_pruefen import (  # noqa: E402
    _grund_abgeschnitten,
    _grund_regelwerk,
)

# ── Was gemeldet werden MUSS ───────────────────────────────────────────────

GEHOERT_NICHT_IN_DIE_LISTE = [
    "Polizeigesetz",
    "Bundesgesetz über die Armee und die Militärverwaltung",
    "Bundesgesetz über die polizeilichen Informationssysteme des Bundes",
    "Verordnung über die Meldestelle für Geldwäscherei",
    "Vereinbarung über die interkantonalen Polizeieinsätze",
    "Polizeikonkordat Nordwestschweiz",
    "Konzept für eine interkantonale Vereinbarung über den Datenaustausch",
    "Polizei-Abfrageplattform",
    "Polizeiliche Kriminalstatistik",
    "Informatiksicherheitsbeauftragter des Departements",
    "Kommunikationsnetz der Bundesverwaltung",
]


@pytest.mark.parametrize("term", GEHOERT_NICHT_IN_DIE_LISTE)
def test_regelwerke_und_systeme_werden_gemeldet(term: str) -> None:
    """Alle elf stammen aus dem Produktivbestand vom 2026-08-24."""
    assert _grund_regelwerk(term) is not None, f"{term!r} wurde nicht gemeldet"


def test_abgeschnittener_eintrag_wird_gemeldet() -> None:
    """Der Torso aus dem Import von PR #4.

    Das Quellglossar traegt an dieser Stelle einen harten Zeilenumbruch mitten
    im Namen; der Import hat ihn nicht geglaettet und den Rest verloren.
    """
    assert _grund_abgeschnitten("Konkordat über die polizeiliche") is not None


def test_eintrag_der_auf_einen_artikel_endet_wird_gemeldet() -> None:
    assert _grund_abgeschnitten("Bundesamt für") is not None


# ── Was NICHT gemeldet werden darf ─────────────────────────────────────────

GEHOERT_IN_DIE_LISTE = [
    # Behoerden
    "Meldestelle für Geldwäscherei",
    "Bundesamt für Polizei",
    "Kantonspolizei Freiburg",
    "Eidgenössisches Justiz- und Polizeidepartement",
    "Konferenz der Kantonalen Polizeikommandantinnen und -kommandanten der Schweiz",
    "Schweizerisches Polizei-Institut",
    "Interkantonale Polizeischule Hitzkirch",
    "Zentrale Ausgleichsstelle",
    # Firmennamen — enden auf kein Behoerdensuffix und sind trotzdem richtig
    "Swisscom (Schweiz) AG",
    "Threema GmbH",
    "Waffen Glauser AG",
    "ZHAW School of Management and Law",
    "Université de Lausanne",
    # Kleingeschriebene Enden, die trotzdem vollstaendige Namen sind. Alle
    # aus dem Produktivbestand vom 2026-08-24; eine fruehere, weiter gefasste
    # Fassung der Torso-Pruefung hat sie samt und sonders gemeldet.
    "Police cantonale jurassienne",
    "Police neuchâteloise",
    "Police du chablais vaudois",
    "smart digitalization group",
    "sintegrA zürich",
    "VBS - Bundesamt für Rüstung armasuisse",
    "Verein eCH",
    "SwissLegal asg.advocati",
    # Franzoesisch: 'des' ist dort kein deutscher Artikel, und der Name endet
    # regelgerecht klein. Die Schweiz ist mehrsprachig, die Liste auch.
    "Centrale d'engagement et de traitement des alarmes",
    "Service informatique de l'Entité neuchâteloise",
]


@pytest.mark.parametrize("term", GEHOERT_IN_DIE_LISTE)
def test_richtige_eintraege_werden_nicht_gemeldet(term: str) -> None:
    """Der teurere Fehler.

    Wer eine richtige Behoerde meldet, verleitet zum Loeschen — und dann steht
    ihr Name wieder im Klartext in jedem Dokument. Firmennamen sind hier der
    heikle Fall: Sie enden auf kein deutsches Behoerdensuffix, und eine
    Pruefung, die 'ist das eine Organisation?' fragte, meldete sie alle.
    """
    assert _grund_regelwerk(term) is None, f"{term!r} faelschlich gemeldet"
    assert _grund_abgeschnitten(term) is None, f"{term!r} faelschlich gemeldet"


def test_meldestelle_und_ihre_verordnung_werden_getrennt() -> None:
    """Das Paar, an dem jede Substring-Suche scheitert.

    Beide enthalten ``Meldestelle`` — aber nur eines *ist* eine.
    """
    assert _grund_regelwerk("Meldestelle für Geldwäscherei") is None
    assert _grund_regelwerk("Verordnung über die Meldestelle für Geldwäscherei")


def test_einwortiger_eintrag_gilt_nie_als_abgeschnitten() -> None:
    """``Kapo`` ist kurz, aber kein Bruchstueck."""
    assert _grund_abgeschnitten("Kapo") is None
    assert _grund_abgeschnitten("Threema") is None
