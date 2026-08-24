#!/usr/bin/env python3
r"""Meldet Eintraege der Begriffsliste, die dort vermutlich nicht hingehoeren.

Anlass ist ein Befund vom 2026-08-24 an der Produktivliste: Unter 669
Eintraegen standen 14, die keine Organisation benennen, aber als
``ORGANIZATION`` gefuehrt wurden — vier Bundesgesetze, das ``Polizeigesetz``,
eine Verordnung, zwei Konkordate, zwei Rollen, ein Kommunikationsnetz und ein
am Zeilenumbruch **abgeschnittener** Torso (``Konkordat über die
polizeiliche``).

Warum das teuer ist
-------------------
Ein Begriff in der Begriffsliste ist Ebene 0: Er wird mit **voller Konfidenz**
ersetzt und schlaegt jede Heuristik (siehe
``backend/engine/recognizers/custom_terms.py``). Ein PTI-Dokument, das sich
„auf das Polizeigesetz" stuetzt, bekommt dort ``[[ORGANIZATION_7]]``. Das
Dokument ist damit **verfaelscht, nicht geschuetzt** — und der Fehler faellt
niemandem auf, weil das Ergebnis genau so aussieht, wie ein pseudonymisiertes
Dokument aussehen soll.

Dieses Skript **schreibt nichts und kann nichts loeschen.** Das ist kein
Versehen, sondern die Bauart: Ein Werkzeug ohne Schreibpfad loescht auch nicht
versehentlich. Was es findet, entfernt ein Mensch in der Oberflaeche unter
„Begriffsliste" — dort steht die Entscheidung, und dort ist sie sichtbar.

Die zwei Pruefungen
-------------------
**1. Kopfnomen benennt kein Organ.** Im Deutschen sagt das Kopfnomen, was ein
Name als Ganzes benennt (Begruendung ausfuehrlich in
``scripts/behoerden_aus_glossar.py``). Endet es auf ``gesetz``, ``verordnung``,
``konkordat``, ``plattform``, ``beauftragter`` …, ist der Eintrag ein
Regelwerk, ein System oder eine Rolle — keine Stelle.

**2. Der Eintrag sieht abgeschnitten aus.** Ein Name, der auf eine
Praeposition, einen Artikel oder ein kleingeschriebenes Wort endet, ist mit
hoher Wahrscheinlichkeit ein Bruchstueck. Genau so entstand ``Konkordat über
die polizeiliche``: Das Quellglossar traegt an dieser Stelle einen harten
Zeilenumbruch mitten im Namen, und der Import von PR #4 hat ihn nicht
geglaettet. Ein solcher Torso trifft im Betrieb entweder nie — oder, schlimmer,
an einer unerwarteten Stelle.

Was das Skript NICHT beurteilen kann
-------------------------------------
Firmennamen. ``Swisscom (Schweiz) AG`` oder ``Threema GmbH`` enden auf kein
deutsches Behoerdensuffix und sind trotzdem voellig richtige Eintraege. Die
Pruefung fragt deshalb nicht „ist das eine Organisation?", sondern die viel
engere Frage „benennt das Kopfnomen ausdruecklich etwas anderes?". Nur so
bleibt die Trefferliste kurz genug, dass ein Mensch sie wirklich durchsieht.

Aufruf
------
Die Datenbank ist nur ueber das LAN erreichbar; das Skript laeuft auf dem
Anwendungsknoten. **Die Umgebungsvariablen muss der Aufruf selbst holen** —
eine SSH-Sitzung erbt die Dashboard-Variablen nicht, daran ist am 2026-08-19
der Deploy von PR #8 gescheitert::

    ssh -i ~/.ssh/jelastic_ci -p 3022 <Knoten>-<Konto>@gate.jpc.infomaniak.com \\
      'set -a; . /.jelenv; set +a; cd /home/jelastic/ROOT && \\
       python scripts/begriffsliste_pruefen.py'

Lokal gegen eine Testdatenbank::

    python scripts/begriffsliste_pruefen.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from behoerden_aus_glossar import (  # noqa: E402
    PRAEPOSITIONEN,
    kopfkandidaten,
)
from sqlalchemy import select  # noqa: E402

from backend.core.deps import DEFAULT_TENANT_NAME  # noqa: E402
from backend.db import get_sessionmaker  # noqa: E402
from backend.vault.models import CustomTerm, Tenant  # noqa: E402

#: Endungen eines Kopfnomens, die ausdruecklich **kein** Organ benennen.
#:
#: Bewusst eine Positivliste des Falschen statt einer Negativliste des
#: Richtigen: Was eine Organisation ist, laesst sich nicht aufzaehlen (jede
#: Firma heisst anders), was ein Gesetz oder ein System ist, dagegen schon.
NICHT_ORGAN: Tuple[str, ...] = (
    "gesetz",
    "verordnung",
    "vereinbarung",
    "konkordat",
    "reglement",
    "richtlinie",
    "weisung",
    "konzept",
    "plattform",
    "statistik",
    "register",
    "schnittstelle",
    "sammlung",
    "blatt",
    "protokoll",
    "vertrag",
    "abkommen",
    "verfahren",
    "handbuch",
    "bericht",
    "kongress",
    "beauftragter",
    "beauftragte",
    "kommunikationsnetz",
)

#: Ein Name, der so endet, ist mit hoher Wahrscheinlichkeit abgeschnitten.
#: Artikel und Praepositionen stehen nie am Ende eines Eigennamens.
ENDET_NIE_AUF = PRAEPOSITIONEN

#: Woerter, an denen ein franzoesischer Name erkennbar ist.
#:
#: Die Schweiz ist mehrsprachig, und die Begriffsliste enthaelt entsprechend
#: ``Police neuchâteloise`` und ``Centrale d'engagement et de traitement des
#: alarmes``. Franzoesische Namen enden regelmaessig kleingeschrieben, und
#: ``des`` ist zugleich ein deutscher Artikel — die Torso-Pruefung schlug
#: deshalb bei ihnen an. Wo ein franzoesisches Funktionswort steht, greift sie
#: nicht mehr.
FRANZOESISCHE_MARKER: Tuple[str, ...] = (" de ", " du ", " et ", " la ", " le ", "d'")


def _grund_regelwerk(term: str) -> str | None:
    """Benennt eines der bestimmenden Woerter ausdruecklich kein Organ.

    Geprueft werden **alle** Woerter vor der ersten Praeposition, nicht nur
    eines. Bei ``Polizeiliche Kriminalstatistik`` steht das entscheidende Wort
    an zweiter Stelle, bei ``Polizeigesetz`` an erster — welches der Kopf ist,
    laesst sich ohne Wortartenerkennung nicht sagen.

    Grosszuegig zu pruefen ist hier ungefaehrlich, weil die Endungen in
    :data:`NICHT_ORGAN` ausdruecklich etwas anderes benennen: Ein Firmenname
    enthaelt kein ``gesetz`` und keine ``verordnung``. Ab der Praeposition
    steht ohnehin nur noch, *wofuer* die Stelle zustaendig ist — ``Bundesamt
    für Statistik`` bleibt deshalb unbehelligt.
    """
    for wort in kopfkandidaten(term):
        klein = wort.lower()
        for endung in NICHT_ORGAN:
            if klein.endswith(endung):
                return f"{wort!r} endet auf {endung!r}"
    return None


def _grund_abgeschnitten(term: str) -> str | None:
    """Meldet, wenn der Eintrag wie ein Bruchstueck aussieht.

    Zwei Anzeichen, beide eng gefasst:

    1. Der Name endet auf einen **Artikel oder eine Praeposition**
       (``Bundesamt für``). Das kommt am Ende eines Eigennamens nie vor.
    2. Der Name endet auf ein kleingeschriebenes Wort, **dem ein Artikel
       vorangeht** (``Konkordat über die polizeiliche``). Dann ist das letzte
       Wort ein Adjektiv, dem sein Nomen fehlt.

    Der zweite Punkt war zuerst weiter gefasst — „endet kleingeschrieben"
    allein — und meldete am Produktivbestand ein Dutzend richtiger Eintraege:
    franzoesische Polizeinamen (``Police neuchâteloise``), Marken mit
    Kleinschreibung (``smart digitalization group``, ``sintegrA zürich``) und
    ``VBS - Bundesamt für Rüstung armasuisse``. Die Annahme „ein Name endet
    gross" gilt nur im Deutschen — und dort auch nicht immer. Der
    vorangehende Artikel ist das verlaessliche Signal, nicht die
    Kleinschreibung.
    """
    woerter = term.split()
    if len(woerter) < 2:
        return None
    letztes = woerter[-1].strip(".,;:")
    if letztes.lower() in ENDET_NIE_AUF:
        return f"endet auf {letztes!r} — Artikel oder Praeposition"
    if any(m in f" {term.lower()} " for m in FRANZOESISCHE_MARKER):
        return None
    vorletztes = woerter[-2].strip(".,;:").lower()
    if letztes and letztes[0].islower() and vorletztes in ENDET_NIE_AUF:
        return (
            f"endet auf {letztes!r} nach dem Artikel {vorletztes!r} — "
            "dem Adjektiv fehlt sein Nomen"
        )
    return None


def pruefe(terms: List[CustomTerm]) -> List[Tuple[CustomTerm, str]]:
    """Liefert die verdaechtigen Eintraege samt Begruendung."""
    befunde: List[Tuple[CustomTerm, str]] = []
    for t in terms:
        grund = _grund_regelwerk(t.term) or _grund_abgeschnitten(t.term)
        if grund:
            befunde.append((t, grund))
    return befunde


def _lade(mandant: str) -> List[CustomTerm]:
    """Liest die Begriffsliste eines Mandanten — ausschliesslich lesend."""
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.name == mandant)
        ).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"Mandant {mandant!r} nicht gefunden.")
        return list(
            session.execute(
                select(CustomTerm).where(CustomTerm.tenant_id == tenant.id)
            ).scalars()
        )


def main(argv: List[str] | None = None) -> int:
    """Prueft die Begriffsliste und meldet, was dort nicht hingehoert."""
    parser = argparse.ArgumentParser(
        description=(
            "Meldet Eintraege der Begriffsliste, die keine Organisation "
            "benennen. Schreibt nichts."
        )
    )
    parser.add_argument("--mandant", default=DEFAULT_TENANT_NAME)
    args = parser.parse_args(argv)

    terms = _lade(args.mandant)
    befunde = pruefe(terms)

    typen: dict[str, int] = {}
    for t in terms:
        typen[t.entity_type] = typen.get(t.entity_type, 0) + 1

    print(f"Begriffsliste {args.mandant!r}: {len(terms)} Eintraege")
    for typ, anzahl in sorted(typen.items()):
        print(f"  {typ:16} {anzahl}")

    if not befunde:
        print("\nKeine verdaechtigen Eintraege. Nichts zu tun.")
        return 0

    print(f"\n=== VERDAECHTIG ({len(befunde)}) ===")
    print(
        "  Diese Eintraege werden mit voller Konfidenz ersetzt. Was hier zu\n"
        "  Unrecht steht, verfaelscht jedes Dokument, in dem es vorkommt.\n"
        "  Entfernen in der Oberflaeche unter 'Begriffsliste'.\n"
    )
    for t, grund in sorted(befunde, key=lambda p: p[0].term.lower()):
        print(f"  {t.id}")
        print(f"      {t.entity_type:14} {t.term}")
        print(f"      Grund: {grund}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
