#!/usr/bin/env python3
"""Einmaliger Import der Begriffsliste aus den Schwestersystemen.

Speist Projekt-, Produkt- und Organisationsnamen aus dem CRM/PPM und dem
Translator-Glossar in die Begriffsliste (Tabelle ``custom_terms``) ein. Die
Begriffsliste ist Ebene 0 der Erkennung (siehe ``backend/routers/terms.py``):
was dort steht, wird mit voller Konfidenz ersetzt und schlägt jede Heuristik
— darum die vergleichsweise strenge Validierung hier.

Warum dieses Werkzeug auf dem Server laufen muss
--------------------------------------------------
Die Datenbank ist ausschliesslich über die interne LAN-Adresse erreichbar
(siehe ``backend/db.py``: eigene, nicht öffentlich exponierte Datenbank,
bewusst getrennt von der geteilten ``pti-db``). Dieses Skript verbindet sich
direkt über ``backend.db.get_sessionmaker()`` und damit über dieselbe
``DATABASE_URL`` wie die Anwendung selbst — es läuft deshalb auf dem
Anwendungsknoten, nicht vom Arbeitsplatz aus. Ein Aufruf von ausserhalb des
LAN scheitert am Verbindungsaufbau, nicht an einer bewussten Sperre.

Eingabeformat
-------------
Eine JSON-Datei mit zwei Listen, ``sicher`` und ``zu_pruefen``. Jeder Eintrag
ist ein Objekt mit ``term``, ``entity_type``, ``match_mode``, optional
``replacement`` (festes Pseudonym, sonst wird gezählt) und optional
``zu_pruefen`` (bool, informativ) sowie ``quellen`` (Liste von
Herkunftssystemen, nur zur Nachvollziehbarkeit — es gibt in ``custom_terms``
keine Spalte dafür, das Skript liest das Feld also, speichert es aber nicht).
Massgeblich für die Zuordnung "sicher" vs. "zu_pruefen" ist die Liste, in der
ein Eintrag steht, nicht das ``zu_pruefen``-Feld im Objekt selbst.

Verhalten
---------
* **Standard:** nur ``sicher`` wird importiert. ``zu_pruefen`` enthält kurze
  Begriffe (3–6 Zeichen), die eher Fehlalarme erzeugen — sie kommen nur mit
  ``--auch-zu-pruefen`` mit. Das ist Absicht, keine Lücke.
* **``--dry-run``** validiert und zählt wie ein echter Lauf, schreibt aber
  nichts. Gedacht als Normalfall beim ersten Aufruf gegen eine neue Datei.
* **Idempotent.** Gleichheit über ``(tenant_id, term)`` — dieselbe Prüfung wie
  in ``backend.routers.terms.create_term``. Ein zweiter Lauf über dieselbe
  Datei legt nichts doppelt an; bereits vorhandene Begriffe werden
  übersprungen und gezählt, nicht überschrieben. Das gilt auch innerhalb
  *eines* Laufs: kommt derselbe Begriff zweimal in der Datei vor (in den 671
  Testdaten z. B. "POLAP" als PTI_PRODUKT und als PTI_PROJEKT), gewinnt der
  erste Treffer, der zweite zählt als Dublette.
* Bestehende Einträge werden **nie** überschrieben, auch nicht mit
  abweichendem ``entity_type`` oder ``match_mode``. Eine inhaltliche
  Korrektur ist Sache der Oberfläche (``/api/v1/terms``), nicht dieses
  Einmal-Werkzeugs.

Validierung
-----------
Vor dem Schreiben muss je Eintrag gelten:

* ``entity_type`` steht im Katalog ``backend.engine.entities.ALL_ENTITIES``.
  Ausnahme: ``PTI_PROJEKT`` und ``PTI_PRODUKT`` gelten als gültig, auch wenn
  sie dort (noch) fehlen — ein paralleler Arbeitsstrang ergänzt sie im
  Katalog. Fehlen sie beim Testen, gibt es dafür nur eine Warnung in der
  Zusammenfassung, keinen Abbruch und keine Ablehnung der betroffenen
  Einträge.
* ``match_mode`` steht in ``backend.routers.terms.MATCH_MODES``.
* ``term`` ist nach dem Trimmen mindestens drei Zeichen lang.
* ``replacement`` ist, falls gesetzt, höchstens 100 Zeichen lang und im
  Mandanten noch nicht vergeben. Ein doppeltes festes Pseudonym wird als
  ungültiger Eintrag gemeldet und nicht angelegt: zwei Klartexte hinter
  demselben Symbol wären nach der Rückführung nicht mehr zu trennen.

Was diese Prüfungen nicht erfüllt, wird abgelehnt und in der Zusammenfassung
aufgeführt — der restliche Import läuft trotzdem weiter.

Aufruf
------
::

    python scripts/begriffe_importieren.py --datei begriffe.json --dry-run
    python scripts/begriffe_importieren.py --datei begriffe.json
    python scripts/begriffe_importieren.py --datei begriffe.json --auch-zu-pruefen
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Wurzel des Repositories — eine Ebene über ``scripts/``. Wird auf den
#: Suchpfad gelegt, damit ``python scripts/begriffe_importieren.py`` auch
#: ohne Installationsschritt funktioniert (dasselbe Muster wie in
#: ``scripts/golden_set_report.py``).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backend.core.deps import DEFAULT_TENANT_NAME, get_tenant  # noqa: E402
from backend.db import get_sessionmaker  # noqa: E402
from backend.engine import entities as ents  # noqa: E402
from backend.routers.terms import MATCH_MODES  # noqa: E402
from backend.vault.models import CustomTerm, Tenant  # noqa: E402

#: Mindestlänge eines Begriffs nach dem Trimmen (Fachvorgabe dieses Imports).
MIN_TERM_LAENGE = 3

#: Entitätstypen, die für diesen Import als gültig gelten, auch wenn sie im
#: Katalog von ``backend/engine/entities.py`` zum Zeitpunkt des Aufrufs noch
#: fehlen sollten. Siehe Docstring oben — ein paralleler Arbeitsstrang pflegt
#: sie dort nach.
ERWARTETE_ZUSATZTYPEN = frozenset({"PTI_PROJEKT", "PTI_PRODUKT"})

#: Die beiden Listen, die die Eingabedatei tragen muss.
LISTENSCHLUESSEL = ("sicher", "zu_pruefen")

#: Obergrenze fuer ein festes Pseudonym — die Breite von
#: ``mapping_entries.pseudonym``, in das der Wert unveraendert uebernommen wird.
MAX_REPLACEMENT_LAENGE = 100


@dataclass(frozen=True)
class Begriff:
    """Ein geprüfter, importierbarer Eintrag der Begriffsliste."""

    term: str
    entity_type: str
    match_mode: str
    herkunftsliste: str  # "sicher" oder "zu_pruefen" — nur für die Anzeige.
    #: Festes Pseudonym, optional. ``None`` heisst: gezähltes Symbol wie sonst.
    replacement: str | None = None


@dataclass
class Zusammenfassung:
    """Zählwerk eines Importlaufs, für die Abschlussausgabe."""

    angelegt: dict[str, int] = field(default_factory=dict)
    bestehend_uebersprungen: dict[str, int] = field(default_factory=dict)
    datei_dubletten: int = 0
    ungueltig: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    katalog_warnungen: set[str] = field(default_factory=set)

    def zaehle_angelegt(self, entity_type: str) -> None:
        """Erhöht den Zähler für neu angelegte Begriffe eines Typs."""
        self.angelegt[entity_type] = self.angelegt.get(entity_type, 0) + 1

    def zaehle_bestehend(self, entity_type: str) -> None:
        """Erhöht den Zähler für bereits vorhandene Begriffe eines Typs."""
        self.bestehend_uebersprungen[entity_type] = (
            self.bestehend_uebersprungen.get(entity_type, 0) + 1
        )

    @property
    def anzahl_angelegt(self) -> int:
        """Gesamtzahl neu angelegter Begriffe über alle Typen."""
        return sum(self.angelegt.values())

    @property
    def anzahl_bestehend(self) -> int:
        """Gesamtzahl übersprungener, bereits vorhandener Begriffe."""
        return sum(self.bestehend_uebersprungen.values())


# ── Laden und Validieren ────────────────────────────────────────────────────


def lade_datei(pfad: Path) -> dict[str, Any]:
    """Liest und parst die Eingabedatei, mit verständlichen Fehlermeldungen.

    Bricht mit ``SystemExit`` ab, wenn die Datei fehlt, kein gültiges JSON
    enthält oder nicht die erwarteten Listen ``sicher``/``zu_pruefen`` trägt
    — an dieser Stelle ist ein harter Abbruch richtig, ein halb eingelesener
    Import wäre nur verwirrend.
    """
    if not pfad.is_file():
        raise SystemExit(f"Datei nicht gefunden: {pfad}")

    try:
        rohtext = pfad.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            f"Datei ist nicht UTF-8-kodiert: {pfad} ({exc})"
        ) from exc

    try:
        daten = json.loads(rohtext)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Ungültiges JSON in {pfad}: {exc}") from exc

    if not isinstance(daten, dict):
        raise SystemExit(
            "Die Eingabedatei muss ein JSON-Objekt mit den Schlüsseln "
            f"{LISTENSCHLUESSEL} sein."
        )

    for schluessel in LISTENSCHLUESSEL:
        if schluessel not in daten:
            raise SystemExit(
                f"Der Eingabedatei fehlt die Liste '{schluessel}'."
            )
        if not isinstance(daten[schluessel], list):
            raise SystemExit(f"'{schluessel}' muss eine Liste sein.")

    return daten


def _gueltige_entitaetstypen() -> frozenset[str]:
    """Katalogtypen plus die noch nicht nachgeführten PTI_-Zusatztypen."""
    return frozenset(ents.ALL_ENTITIES) | ERWARTETE_ZUSATZTYPEN


def pruefe_eintrag(
    rohdaten: dict[str, Any], herkunftsliste: str, warnungen: set[str]
) -> tuple[Begriff | None, str | None]:
    """Prüft einen einzelnen Roheintrag.

    Gibt entweder ``(Begriff, None)`` bei einem gültigen Eintrag zurück, oder
    ``(None, Grund)`` bei einem ungültigen. Ein Katalog-Zusatztyp
    (:data:`ERWARTETE_ZUSATZTYPEN`), der im Katalog noch fehlt, gilt als
    gültig, trägt aber zu ``warnungen`` bei — siehe Modul-Docstring.
    """
    term = str(rohdaten.get("term", "")).strip()
    entity_type = str(rohdaten.get("entity_type", "")).strip()
    match_mode = str(rohdaten.get("match_mode", "")).strip()

    if len(term) < MIN_TERM_LAENGE:
        return None, (
            f"Begriff nach dem Trimmen kürzer als {MIN_TERM_LAENGE} Zeichen: "
            f"{term!r}"
        )

    gueltige_typen = _gueltige_entitaetstypen()
    if entity_type not in gueltige_typen:
        return None, f"Unbekannter entity_type {entity_type!r} für Begriff {term!r}"

    if entity_type in ERWARTETE_ZUSATZTYPEN and entity_type not in ents.ALL_ENTITIES:
        warnungen.add(entity_type)

    if match_mode not in MATCH_MODES:
        return None, (
            f"Ungültiger match_mode {match_mode!r} für Begriff {term!r} "
            f"(gültig: {MATCH_MODES})"
        )

    replacement = str(rohdaten.get("replacement") or "").strip() or None
    if replacement is not None and len(replacement) > MAX_REPLACEMENT_LAENGE:
        # Dieselbe Grenze wie in der API, und aus demselben Grund: der Wert
        # landet eins zu eins in ``mapping_entries.pseudonym`` (VARCHAR(100)).
        return None, (
            f"Festes Pseudonym für Begriff {term!r} ist länger als "
            f"{MAX_REPLACEMENT_LAENGE} Zeichen"
        )

    return Begriff(
        term=term,
        entity_type=entity_type,
        match_mode=match_mode,
        herkunftsliste=herkunftsliste,
        replacement=replacement,
    ), None


def sammle_kandidaten(
    daten: dict[str, Any], auch_zu_pruefen: bool
) -> list[tuple[dict[str, Any], str]]:
    """Wählt die zu importierenden Rohlisten aus (Standard vs. ``--auch-zu-pruefen``).

    Reihenfolge ``sicher`` vor ``zu_pruefen``: bei einer Dublette zwischen
    beiden Listen (z. B. derselbe Begriff mit unterschiedlichem Typ) soll die
    geprüfte Angabe aus ``sicher`` gewinnen.
    """
    kandidaten = [(roh, "sicher") for roh in daten["sicher"]]
    if auch_zu_pruefen:
        kandidaten += [(roh, "zu_pruefen") for roh in daten["zu_pruefen"]]
    return kandidaten


def validiere_alle(
    kandidaten: list[tuple[dict[str, Any], str]],
) -> tuple[list[Begriff], list[tuple[dict[str, Any], str]], set[str]]:
    """Prüft alle Kandidaten und trennt sie in gültig/ungültig.

    Returns
    -------
    tuple
        ``(gueltige_begriffe, (rohdaten, grund)-Paare der abgelehnten,
        Menge der Katalog-Warnungen)``.
    """
    gueltig: list[Begriff] = []
    ungueltig: list[tuple[dict[str, Any], str]] = []
    warnungen: set[str] = set()

    for roh, herkunft in kandidaten:
        begriff, grund = pruefe_eintrag(roh, herkunft, warnungen)
        if begriff is None:
            ungueltig.append((roh, grund or "unbekannter Grund"))
        else:
            gueltig.append(begriff)

    return gueltig, ungueltig, warnungen


# ── Import ───────────────────────────────────────────────────────────────


def importiere(
    session: Session,
    tenant: Tenant,
    begriffe: list[Begriff],
    *,
    dry_run: bool,
) -> Zusammenfassung:
    """Legt die übergebenen, bereits geprüften Begriffe an.

    Idempotent über ``(tenant_id, term)`` — siehe Modul-Docstring. Prüft dafür
    einmal vorab den vorhandenen Bestand des Mandanten und führt zusätzlich
    eine laufende Menge der in diesem Aufruf bereits vergebenen Begriffe, damit
    auch Dubletten *innerhalb* der Eingabedatei nicht doppelt angelegt werden.

    Im ``dry_run`` wird identisch gezählt, aber nichts der Datenbank
    hinzugefügt und nicht committet.
    """
    zusammenfassung = Zusammenfassung()

    bestehende_begriffe = set(
        session.execute(
            select(CustomTerm.term).where(CustomTerm.tenant_id == tenant.id)
        ).scalars()
    )
    # Feste Pseudonyme muessen je Mandant eindeutig bleiben: zwei Klartexte
    # hinter demselben Symbol waeren nach der Rueckfuehrung nicht mehr zu
    # trennen. Die API lehnt das mit 409 ab; hier wird der Eintrag als
    # ungueltig gemeldet, statt die Ambiguitaet still anzulegen.
    vergebene_pseudonyme = set(
        session.execute(
            select(CustomTerm.replacement).where(
                CustomTerm.tenant_id == tenant.id,
                CustomTerm.replacement.is_not(None),
            )
        ).scalars()
    )
    in_diesem_lauf_vergeben: set[str] = set()

    for begriff in begriffe:
        if begriff.term in bestehende_begriffe:
            zusammenfassung.zaehle_bestehend(begriff.entity_type)
            continue
        if begriff.term in in_diesem_lauf_vergeben:
            zusammenfassung.datei_dubletten += 1
            continue
        if begriff.replacement and begriff.replacement in vergebene_pseudonyme:
            zusammenfassung.ungueltig.append(
                (
                    {"term": begriff.term, "replacement": begriff.replacement},
                    f"Festes Pseudonym {begriff.replacement!r} ist bereits vergeben",
                )
            )
            continue

        if begriff.replacement:
            vergebene_pseudonyme.add(begriff.replacement)
        in_diesem_lauf_vergeben.add(begriff.term)
        zusammenfassung.zaehle_angelegt(begriff.entity_type)

        if not dry_run:
            session.add(
                CustomTerm(
                    tenant_id=tenant.id,
                    term=begriff.term,
                    entity_type=begriff.entity_type,
                    match_mode=begriff.match_mode,
                    replacement=begriff.replacement,
                )
            )

    if not dry_run:
        session.commit()

    return zusammenfassung


# ── Ausgabe ──────────────────────────────────────────────────────────────


def drucke_zusammenfassung(
    zusammenfassung: Zusammenfassung, *, dry_run: bool
) -> None:
    """Gibt die Abschlussübersicht des Laufs auf der Konsole aus."""
    titel = "Ergebnis (Probelauf, nichts geschrieben)" if dry_run else "Ergebnis"
    print(f"\n{titel}")
    print("=" * len(titel))

    print(f"\nNeu angelegt: {zusammenfassung.anzahl_angelegt}")
    for entity_type in sorted(zusammenfassung.angelegt):
        print(f"  {entity_type}: {zusammenfassung.angelegt[entity_type]}")

    print(f"\nÜbersprungen, bereits vorhanden: {zusammenfassung.anzahl_bestehend}")
    for entity_type in sorted(zusammenfassung.bestehend_uebersprungen):
        anzahl = zusammenfassung.bestehend_uebersprungen[entity_type]
        print(f"  {entity_type}: {anzahl}")

    print(f"\nDubletten innerhalb der Datei: {zusammenfassung.datei_dubletten}")

    print(f"\nAbgelehnt (ungültig): {len(zusammenfassung.ungueltig)}")
    for roh, grund in zusammenfassung.ungueltig:
        print(f"  {grund} — {roh!r}")

    if zusammenfassung.katalog_warnungen:
        print("\nWarnung: folgende Typen fehlen noch im Katalog "
              "backend/engine/entities.py, wurden aber importiert:")
        for entity_type in sorted(zusammenfassung.katalog_warnungen):
            print(f"  {entity_type}")


# ── Kommandozeile ────────────────────────────────────────────────────────


def baue_parser() -> argparse.ArgumentParser:
    """Baut den Argumentparser des Skripts."""
    parser = argparse.ArgumentParser(
        description=(
            "Importiert Projekt-, Produkt- und Organisationsnamen aus dem "
            "CRM/PPM und dem Translator-Glossar in die Begriffsliste."
        )
    )
    parser.add_argument(
        "--datei",
        required=True,
        type=Path,
        help="Pfad zur JSON-Datei mit den Listen 'sicher' und 'zu_pruefen'.",
    )
    parser.add_argument(
        "--auch-zu-pruefen",
        action="store_true",
        help=(
            "Importiert zusätzlich die Liste 'zu_pruefen' (kurze, "
            "fehlalarmträchtige Begriffe). Standardmässig aussen vor."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zeigt an, was geschähe, schreibt aber nichts in die Datenbank.",
    )
    parser.add_argument(
        "--mandant",
        default=DEFAULT_TENANT_NAME,
        help=(
            "Name des Mandanten. In Phase 1 gibt es nur den einen Mandanten "
            f"'{DEFAULT_TENANT_NAME}' (siehe backend/core/deps.py) — die "
            "Option dient der Nachvollziehbarkeit im Aufruf, nicht der "
            "Auswahl."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Einstiegspunkt für die Kommandozeile."""
    args = baue_parser().parse_args(argv)

    daten = lade_datei(args.datei)
    kandidaten = sammle_kandidaten(daten, args.auch_zu_pruefen)
    begriffe, ungueltig, warnungen = validiere_alle(kandidaten)

    print(f"Datei: {args.datei}")
    print(
        f"Kandidaten: {len(kandidaten)} "
        f"({'inklusive' if args.auch_zu_pruefen else 'ohne'} 'zu_pruefen')"
    )

    factory = get_sessionmaker()
    session = factory()
    try:
        tenant = get_tenant(session)
        if args.mandant != tenant.name:
            print(
                f"Warnung: --mandant {args.mandant!r} weicht vom aktiven "
                f"Phase-1-Mandanten {tenant.name!r} ab. Es wird trotzdem mit "
                f"{tenant.name!r} gearbeitet — siehe --mandant-Hilfetext."
            )

        zusammenfassung = importiere(session, tenant, begriffe, dry_run=args.dry_run)
        zusammenfassung.ungueltig = ungueltig
        zusammenfassung.katalog_warnungen = warnungen
    finally:
        session.close()

    drucke_zusammenfassung(zusammenfassung, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
