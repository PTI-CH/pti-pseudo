#!/usr/bin/env python3
r"""Behoerdennamen aus dem Translator-Glossar zu einer Importdatei aufbereiten.

Anlass ist ein gemessener Befund des Golden-Sets: ``Meldestelle für
Geldwäscherei`` wird vom Sprachmodell nicht als ``ORGANIZATION`` erkannt.
PTI-Behoerdennamen kennt es nicht, und kein statistisches Modell kann sie
kennen — es gibt an ihnen nichts zu erkennen ausser dem Wissen, dass es
Behoerden sind. Genau dafuer gibt es die Begriffsliste (Ebene 0).

Das Material liegt im Schwester-Repo ``pti-translator`` unter
``backend/data/glossaries.json``: 1099 Eintraege, davon 417 mit einer
ausgeschriebenen Form (``deFull``). Dieses Skript waehlt daraus die Behoerden
aus und schreibt sie in das Format, das ``scripts/begriffe_importieren.py``
erwartet.

Warum die Auswahl nicht trivial ist
-----------------------------------
Ein Begriff in der Begriffsliste wird mit **voller Konfidenz** ersetzt und
schlaegt jede Heuristik (siehe ``backend/engine/recognizers/custom_terms.py``).
Ein falscher Eintrag ist deshalb teuer: stuende ``Polizeigesetz`` in der Liste,
erschiene es fortan in jedem Dokument als ``[[ORGANIZATION_3]]`` — das
Dokument waere verfaelscht, nicht geschuetzt.

Die 417 ausgeschriebenen Formen sind gemischt: echte Behoerden
(``Bundesamt für Polizei``) stehen neben Gesetzen (``Bundesgesetz über die
Armee``), Systemen (``Polizei-Abfrageplattform``), Rollen
(``Informatiksicherheitsbeauftragter des Departements``) und reinen
Fachbegriffen (``Service Level Agreement``). Das Glossar kennt genau einen Tag,
``Abkürzung`` — es gibt also kein Feld, das die Sorte verriete.

Die Kopfnomen-Regel
-------------------
Entscheidend ist **nicht**, ob irgendwo im Namen ein Behoerdenwort vorkommt,
sondern **was der Name als Ganzes benennt**. Im Deutschen sagt das das
Kopfnomen — das erste Nomen vor der ersten Praeposition:

===================================================  ===========  =========
Name                                                 Kopfnomen    Ergebnis
===================================================  ===========  =========
``Bundesamt für Statistik``                          Bundesamt    Behoerde
``Verordnung über die Meldestelle für Geldwäscherei``Verordnung   verworfen
``Meldestelle für Geldwäscherei``                    Meldestelle  Behoerde
``Amtliche Sammlung des Bundesrechts``               Sammlung     verworfen
===================================================  ===========  =========

Das zweite und dritte Beispiel sind das Paar, an dem jede Substring-Suche
scheitert: beide enthalten ``Meldestelle``, aber nur eines *ist* eine.

Weil deutsche Komposita ihren Kopf am **Ende** tragen (``Bundes-amt``,
``Kantons-polizei``), genuegt es, die Endung des Kopfnomens gegen eine Liste
von Organisationssuffixen zu pruefen.

Was das Skript bewusst NICHT tut
--------------------------------
Es entscheidet nicht endgueltig. Die Regel ist gut, aber sie ist eine Regel
ueber Sprache und kennt keine Ausnahmen von sich aus. Die Ausgabe ist deshalb
ein **Vorschlag zur Durchsicht**: ``--bericht`` zeigt beide Seiten der
Entscheidung, damit ein Mensch sie einmal prueft, bevor irgendetwas in die
Datenbank geht. Erst danach wandert die Datei nach
``scripts/begriffe_importieren.py --dry-run``.

Die Abkuerzungen (``de``) kommen nach ``zu_pruefen`` und damit standardmaessig
**nicht** mit: ``GS``, ``AM``, ``MG``, ``Kapo FR`` sehen aus wie Initialen oder
Kantonskuerzel und sind im Golden-Set gezielte Fehlalarm-Fallen.

Aufruf
------
::

    python scripts/behoerden_aus_glossar.py --bericht
    python scripts/behoerden_aus_glossar.py --ziel scripts/data/behoerden.json

Und danach in Produktion
------------------------
Die Datenbank ist nur ueber das LAN erreichbar, der Import laeuft deshalb auf
dem Anwendungsknoten. **Die Umgebungsvariablen muss der Aufruf selbst holen:**
Der Runner laedt ``/.jelenv`` per ``source``, exportiert die Werte aber nicht,
und eine SSH-Sitzung erbt die Dashboard-Variablen des Knotens nicht. Genau
daran ist am 2026-08-19 der Deploy von PR #8 gescheitert
(``RuntimeError: DATABASE_URL ist nicht gesetzt``)::

    ssh -i ~/.ssh/jelastic_ci -p 3022 213705-<KontoID>@gate.jpc.infomaniak.com \\
      'set -a; . /.jelenv; set +a; cd /home/jelastic/ROOT && \\
       python scripts/begriffe_importieren.py \\
         --datei scripts/data/behoerden.json --dry-run'

Erst nach einem sauberen Probelauf derselbe Aufruf ohne ``--dry-run``. Der
Import ist idempotent ueber ``(tenant_id, term)`` und ueberschreibt nie.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

#: Wurzel des Repositories — eine Ebene ueber ``scripts/``. Dasselbe Muster wie
#: in ``scripts/begriffe_importieren.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Der Glossar-Export des Translators. Liegt im Schwester-Repo, nicht hier —
#: er wird gelesen, nie geschrieben.
STANDARD_QUELLE = (
    REPO_ROOT.parent / "pti-translator" / "backend" / "data" / "glossaries.json"
)

STANDARD_ZIEL = REPO_ROOT / "scripts" / "data" / "behoerden.json"

#: Endungen, an denen ein Kopfnomen eine Organisation ausweist.
#:
#: Sortiert nach Haeufigkeit im Material. ``amt`` deckt ``Bundesamt`` und
#: ``Zivilstandsamt`` ab, ``polizei`` die 20 Kantonspolizeien, ``konferenz``
#: die Direktorenkonferenzen — zusammen sind das rund drei Viertel der Treffer.
ORGANISATIONS_ENDUNGEN: tuple[str, ...] = (
    "amt",
    "polizei",
    "kommission",
    "departement",
    "konferenz",
    "direktion",
    "sekretariat",
    "staatsanwaltschaft",
    "anwaltschaft",
    "gericht",
    "gerichtshof",
    "union",
    "gemeinschaft",
    "gruppe",
    "verband",
    "vereinigung",
    "verein",
    "stelle",
    "behörde",
    "institut",
    "hochschule",
    "universität",
    "schule",
    "korps",
    "inspektorat",
    "kanzlei",
    "verwaltung",
    "agentur",
    "stiftung",
    "genossenschaft",
    "zentrum",
    "zentrale",
    "büro",
    "kontrolle",
    "dienst",
    "organisation",
    "rat",
)

#: Woerter, die das Kopfnomen beenden: ab hier beschreibt der Name nur noch,
#: *wofuer* die Stelle zustaendig ist. ``Bundesamt für Statistik`` ist ein Amt,
#: kein Statistikding.
PRAEPOSITIONEN: frozenset[str] = frozenset(
    {
        "für",
        "über",
        "der",
        "des",
        "die",
        "das",
        "den",
        "dem",
        "zur",
        "zum",
        "im",
        "in",
        "von",
        "vom",
        "und",
        "betreffend",
        "auf",
        "bei",
        "mit",
    }
)

#: Zerlegt an Leerzeichen und Satzzeichen, aber **nicht** am Bindestrich —
#: ``Polizei-Abfrageplattform`` ist ein Wort. Der Kopf eines Kompositums steht
#: hinten, deshalb entscheidet dort die Endung ``plattform`` und nicht der
#: vordere Teil ``Polizei``. Ein Split am Bindestrich machte daraus faelschlich
#: eine Behoerde.
_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)


def kopfnomen(name: str) -> str:
    """Das bestimmende Nomen eines deutschen Behoerdennamens.

    Genommen wird das **letzte Wort vor der ersten Praeposition**. Gibt es
    keine Praeposition, ist es das erste Wort: ``Kantonspolizei Zürich`` wird
    von ``Kantonspolizei`` bestimmt, nicht von ``Zürich``.

    Adjektive am Anfang (``Eidgenössisches Departement für …``) werden dadurch
    uebersprungen, ohne dass eine Wortartenerkennung noetig waere — sie stehen
    vor dem Nomen, nicht danach.

    Bindestrich-Komposita bleiben zusammen und werden von ihrem **letzten**
    Glied bestimmt — die allgemeine Kompositumsregel, hier nur sichtbar
    geschrieben.

    Examples
    --------
    >>> kopfnomen("Bundesamt für Statistik")
    'Bundesamt'
    >>> kopfnomen("Verordnung über die Meldestelle für Geldwäscherei")
    'Verordnung'
    >>> kopfnomen("Eidgenössisches Departement für auswärtige Angelegenheiten")
    'Departement'
    >>> kopfnomen("Kantonspolizei Zürich")
    'Kantonspolizei'
    >>> kopfnomen("Polizei-Abfrageplattform")
    'Polizei-Abfrageplattform'
    """
    woerter = _TOKEN.findall(name)
    if not woerter:
        return ""
    vorne: list[str] = []
    for wort in woerter:
        if wort.lower() in PRAEPOSITIONEN:
            break
        vorne.append(wort)
    if not vorne:
        return woerter[0]
    # Ohne Praeposition bestimmt das erste Wort (``Kantonspolizei Zürich``),
    # mit Praeposition das letzte davor (``Eidgenössisches Departement für …``).
    return vorne[-1] if len(vorne) < len(woerter) else vorne[0]


def ist_qualifiziert(name: str) -> bool:
    """Ob der Name eine *bestimmte* Stelle benennt und nicht die Gattung.

    ``Kantonspolizei Bern`` ist eine Behoerde, ``Kantonspolizei`` das Wort
    dafuer. ``Gruppe``, ``Ordnungsdienst`` und ``Telefonkontrolle`` sind
    gewoehnliche Substantive, die in jedem zweiten Satz vorkommen — als
    Begriffsliste-Eintrag wuerden sie mit voller Konfidenz ersetzt und
    zerlegten den Text.

    Als qualifiziert gilt, was aus **mehr als einem Wort** besteht. Das ist
    absichtlich streng: es schickt auch echte Einwort-Behoerden wie
    ``Bundesanwaltschaft`` in die Durchsicht. Der umgekehrte Fehler ist
    teurer — eine zu Unrecht importierte Gattungsbezeichnung faellt erst auf,
    wenn sie ein Dokument bereits verfaelscht hat.

    Examples
    --------
    >>> ist_qualifiziert("Kantonspolizei Bern")
    True
    >>> ist_qualifiziert("Kantonspolizei")
    False
    >>> ist_qualifiziert("Bundesamt für Polizei")
    True
    """
    return len(_TOKEN.findall(name)) > 1


def kopfkandidaten(name: str) -> list[str]:
    """Alle Woerter vor der ersten Praeposition — die moeglichen Koepfe.

    Welches davon der Kopf ist, laesst sich ohne Wortartenerkennung nicht
    entscheiden, und beide Stellungen sind haeufig:

    * ``Kantonspolizei Zürich`` — der Kopf steht **vorn**, hinten praezisiert
      ein Eigenname.
    * ``Eidgenössische Finanzkontrolle`` — der Kopf steht **hinten**, vorn
      steht ein flektiertes Adjektiv.
    * ``Interkantonale Polizeischule Hitzkirch`` — er steht **in der Mitte**,
      zwischen Adjektiv und Ortsname.

    Fuer eine einmalige Aufbereitung von 417 Namen ist ein Wortarten-Tagger zu
    viel Maschinerie. Geprueft wird deshalb jedes Wort des Namensteils vor der
    ersten Praeposition; die Praeposition selbst ist die eigentliche Grenze,
    denn ab ihr steht nur noch, *wofuer* die Stelle zustaendig ist.

    Der Preis ist eine Handvoll grosszuegiger Treffer — ``Nationale
    Bildungsplattform Polizei`` wird wegen des letzten Wortes mitgenommen,
    obwohl es eine Plattform ist. Genau dafuer gibt es die Durchsicht.

    Examples
    --------
    >>> kopfkandidaten("Kantonspolizei Zürich")
    ['Kantonspolizei', 'Zürich']
    >>> kopfkandidaten("Interkantonale Polizeischule Hitzkirch")
    ['Interkantonale', 'Polizeischule', 'Hitzkirch']
    >>> kopfkandidaten("Verordnung über die Meldestelle für Geldwäscherei")
    ['Verordnung']
    """
    woerter = _TOKEN.findall(name)
    if not woerter:
        return []
    vorne: list[str] = []
    for wort in woerter:
        if wort.lower() in PRAEPOSITIONEN:
            break
        vorne.append(wort)
    return vorne or [woerter[0]]


def ist_organisation(name: str) -> bool:
    """Ob der Name eine Stelle benennt und nicht ein Regelwerk oder System.

    Examples
    --------
    >>> ist_organisation("Meldestelle für Geldwäscherei")
    True
    >>> ist_organisation("Verordnung über die Meldestelle für Geldwäscherei")
    False
    >>> ist_organisation("Eidgenössische Finanzkontrolle")
    True
    >>> ist_organisation("Polizei-Abfrageplattform")
    False
    """
    return any(
        kandidat.lower().endswith(endung)
        for kandidat in kopfkandidaten(name)
        for endung in ORGANISATIONS_ENDUNGEN
    )


def lade_eintraege(pfad: Path) -> list[dict[str, Any]]:
    """Liest den Glossar-Export und gibt alle Eintraege aller Glossare zurueck."""
    if not pfad.is_file():
        raise SystemExit(
            f"Glossar nicht gefunden: {pfad}\n"
            "Erwartet wird das Schwester-Repo pti-translator neben diesem hier. "
            "Anderer Ort: --quelle <pfad>"
        )
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except json.JSONDecodeError as fehler:
        raise SystemExit(f"{pfad} ist kein gueltiges JSON: {fehler}") from None
    if not isinstance(daten, list):
        raise SystemExit(f"{pfad} sollte eine Liste von Glossaren enthalten.")
    return [
        e
        for glossar in daten
        for e in glossar.get("entries", [])
        if isinstance(e, dict)
    ]


def _saubere_form(wert: Any) -> str:
    r"""Trimmt und macht Zeilenumbrueche im Glossartext unschaedlich.

    Zwei Eintraege des Materials tragen einen harten Umbruch mitten im Namen
    (``Konkordat über die polizeiliche\\nZusammenarbeit …``). Ungeglaettet
    stuende der Umbruch im Begriff und faende nie ein Vorkommen.
    """
    if not isinstance(wert, str):
        return ""
    return re.sub(r"\s+", " ", wert).strip()


def teile_auf(eintraege: Iterable[dict[str, Any]]) -> tuple[list, list, list, list]:
    """Sortiert die Eintraege in vier Toepfe.

    Returns
    -------
    (behoerden, gattungen, abkuerzungen, verworfen)
        ``behoerden``
            ``(abkuerzung, ausgeschrieben)`` mit organisationswertigem
            Kopfnomen **und** einem Qualifikator — der ``sicher``-Topf.
        ``gattungen``
            Organisationswertig, aber einwortig (``Kantonspolizei``,
            ``Ordnungsdienst``). Gehen nach ``zu_pruefen``.
        ``abkuerzungen``
            Die Kuerzel beider Gruppen. Ebenfalls ``zu_pruefen``.
        ``verworfen``
            Alles uebrige mit ``deFull`` — nur fuer den Bericht, damit die
            Regel nachpruefbar bleibt statt still zu wirken.
    """
    behoerden: list[tuple[str, str]] = []
    gattungen: list[tuple[str, str]] = []
    abkuerzungen: list[tuple[str, str]] = []
    verworfen: list[tuple[str, str]] = []
    for eintrag in eintraege:
        voll = _saubere_form(eintrag.get("deFull"))
        kurz = _saubere_form(eintrag.get("de"))
        if not voll:
            continue
        if not ist_organisation(voll):
            verworfen.append((kurz, voll))
            continue
        (behoerden if ist_qualifiziert(voll) else gattungen).append((kurz, voll))
        if kurz:
            abkuerzungen.append((kurz, voll))
    return behoerden, gattungen, abkuerzungen, verworfen


def baue_importdatei(
    behoerden: list[tuple[str, str]],
    gattungen: list[tuple[str, str]],
    abkuerzungen: list[tuple[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """Baut die Struktur, die ``begriffe_importieren.py`` erwartet.

    ``match_mode`` ist durchgehend ``normalized``: das Glossar schreibt echte
    Umlaute, Dokumente aus dem Betrieb nicht immer. ``exact`` verfehlte
    ``Kantonspolizei Zuerich``.

    Kein ``replacement``: ein festes Pseudonym muesste im Mandanten eindeutig
    sein und braechte hier nichts — ein gezaehltes ``[[ORGANIZATION_n]]``
    genuegt und haelt die Zuordnung im Tresor.
    """
    gesehen: set[str] = set()
    sicher: list[dict[str, Any]] = []
    for _, voll in sorted(behoerden, key=lambda p: p[1].lower()):
        if voll.lower() in gesehen:
            continue
        gesehen.add(voll.lower())
        sicher.append(
            {
                "term": voll,
                "entity_type": "ORGANIZATION",
                "match_mode": "normalized",
                "quellen": ["pti-translator/PTI Glossar"],
            }
        )

    zu_pruefen: list[dict[str, Any]] = []
    for _, voll in sorted(gattungen, key=lambda p: p[1].lower()):
        if voll.lower() in gesehen:
            continue
        gesehen.add(voll.lower())
        zu_pruefen.append(
            {
                "term": voll,
                "entity_type": "ORGANIZATION",
                "match_mode": "normalized",
                "zu_pruefen": True,
                "quellen": ["pti-translator/PTI Glossar"],
                "hinweis": "Gattungsbezeichnung ohne Qualifikator — kommt auch "
                "als gewoehnliches Substantiv vor.",
            }
        )

    gesehen_kurz: set[str] = set()
    for kurz, voll in sorted(abkuerzungen, key=lambda p: p[0].lower()):
        if kurz.lower() in gesehen_kurz:
            continue
        gesehen_kurz.add(kurz.lower())
        zu_pruefen.append(
            {
                "term": kurz,
                "entity_type": "ORGANIZATION",
                # ``exact``: bei einem Kuerzel entscheidet die Schreibweise.
                # ``normalized`` liesse ``as`` auf ``AS`` passen.
                "match_mode": "exact",
                "zu_pruefen": True,
                "quellen": ["pti-translator/PTI Glossar"],
                "hinweis": f"Abkuerzung von {voll!r}",
            }
        )
    return {"sicher": sicher, "zu_pruefen": zu_pruefen}


def drucke_bericht(
    behoerden: list[tuple[str, str]],
    gattungen: list[tuple[str, str]],
    verworfen: list[tuple[str, str]],
) -> None:
    """Zeigt alle drei Seiten der Entscheidung, je mit dem Kopfnomen davor.

    Der Bericht ist der eigentliche Zweck dieses Skripts: Die Regel soll
    nachpruefbar sein, bevor irgendetwas in die Datenbank geht.
    """
    print(f"\n=== SICHER: qualifizierte Behoerden ({len(behoerden)}) ===")
    for kurz, voll in sorted(behoerden, key=lambda p: p[1].lower()):
        print(f"  [{kopfnomen(voll):<18}] {kurz or '—':<12} {voll}")

    print(f"\n=== ZU PRUEFEN: Gattungsbezeichnungen ({len(gattungen)}) ===")
    print("  Organ, aber einwortig — kommt auch als gewoehnliches Substantiv vor.\n")
    for kurz, voll in sorted(gattungen, key=lambda p: p[1].lower()):
        print(f"  [{kopfnomen(voll):<18}] {kurz or '—':<12} {voll}")

    print(f"\n=== VERWORFEN ({len(verworfen)}) ===")
    print("  Kopfnomen benennt kein Organ — Gesetz, System, Rolle, Fachbegriff.\n")
    for kurz, voll in sorted(verworfen, key=lambda p: p[1].lower()):
        print(f"  [{kopfnomen(voll):<18}] {kurz or '—':<12} {voll}")


def main(argv: list[str] | None = None) -> int:
    """Liest das Glossar, teilt auf und schreibt — oder berichtet nur."""
    parser = argparse.ArgumentParser(
        description="Behoerdennamen aus dem Translator-Glossar aufbereiten.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--quelle", type=Path, default=STANDARD_QUELLE)
    parser.add_argument("--ziel", type=Path, default=STANDARD_ZIEL)
    parser.add_argument(
        "--bericht",
        action="store_true",
        help="Beide Seiten der Entscheidung auflisten und NICHTS schreiben.",
    )
    args = parser.parse_args(argv)

    eintraege = lade_eintraege(args.quelle)
    behoerden, gattungen, abkuerzungen, verworfen = teile_auf(eintraege)

    mit_voller_form = len(behoerden) + len(gattungen) + len(verworfen)
    print(
        f"Glossar: {len(eintraege)} Eintraege, "
        f"davon {mit_voller_form} mit voller Form."
    )
    print(f"  sicher     — qualifizierte Behoerden: {len(behoerden)}")
    print(f"  zu_pruefen — Gattungsbezeichnungen:   {len(gattungen)}")
    print(f"  zu_pruefen — Abkuerzungen:            {len(abkuerzungen)}")
    print(f"  verworfen:                            {len(verworfen)}")

    if args.bericht:
        drucke_bericht(behoerden, gattungen, verworfen)
        print("\nNichts geschrieben (--bericht).")
        return 0

    datei = baue_importdatei(behoerden, gattungen, abkuerzungen)
    args.ziel.parent.mkdir(parents=True, exist_ok=True)
    args.ziel.write_text(
        json.dumps(datei, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"\nGeschrieben: {args.ziel}\n"
        f"  sicher:     {len(datei['sicher'])}\n"
        f"  zu_pruefen: {len(datei['zu_pruefen'])}\n\n"
        "Naechster Schritt — erst pruefen, dann importieren:\n"
        f"  python scripts/begriffe_importieren.py --datei {args.ziel} --dry-run"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
