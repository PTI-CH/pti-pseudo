#!/usr/bin/env python3
"""Misst die Erkennungsqualität des PTI-Pseudonymisierers am Golden-Set.

Dieses Modul erfüllt zwei Rollen:

1. **Kommandozeilenwerkzeug** für Menschen, die die Erkennung verbessern
   wollen: ``python scripts/golden_set_report.py`` gibt eine lesbare Tabelle
   mit Precision/Recall/F1 je Entitätstyp aus, dazu die vollständige Liste
   aller verfehlten Erwartungen und aller Fehlalarme mit Dokumentname und
   Ampelstufe (``--dokument NAME`` schränkt auf ein einzelnes Testdokument
   ein).
2. **Bibliothek** für ``backend/tests/test_golden_set.py``: die Auswertung
   (:func:`werte_aus`) läuft dort in einer session-weiten Fixture genau
   einmal, die einzelnen Tests lesen nur noch die Kennzahlen daraus.

Vertrag zum Golden-Set
-----------------------
``golden-set/synthetic/generator.py`` (von einem anderen Arbeitsstrang
gebaut) stellt ``DOKUMENTE: list[Testdokument]`` bereit, mit
``Testdokument(name, dateiname, beschreibung, inhalt, erwartet, verboten)``
und ``Erwartung(text, entity_type, grund="")``. Dieses Modul importiert es
nicht statisch (das Verzeichnis ``golden-set`` fehlt beim Start dieses
Arbeitsstrangs eventuell noch) — siehe :func:`lade_generator`.

Wie gemessen wird
------------------
Je Testdokument: Adapter holen (:func:`backend.adapters.get_adapter`),
Segmente extrahieren, Erkennung laufen lassen
(:func:`backend.pipeline.detect_in_segments`). Der Vergleich läuft über das
Paar ``(text, entity_type)``, nicht über Zeichenpositionen — die hängen vom
Adapter ab und wären zu spröde. Bewertet werden ausschliesslich die Typen in
:data:`BEWERTETE_TYPEN`; alles andere (``DATE_TIME``, ``URL`` …) wird
ignoriert, sonst müsste das Golden-Set jedes Datum annotieren.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

#: Wurzel des Repositories — zwei Ebenen über dieser Datei (``scripts/…``).
#: Wird explizit auf den Suchpfad gelegt, damit das Skript sowohl per
#: ``python scripts/golden_set_report.py`` (dann läge nur ``scripts/`` auf dem
#: Pfad) als auch als Testabhängigkeit funktioniert.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import pipeline  # noqa: E402
from backend.adapters import get_adapter  # noqa: E402

#: Nur diese Typen fliessen in Precision/Recall ein (Fachkonzept §13.1/13.2).
#: Ein Fund ausserhalb dieser Menge wird weder als Treffer noch als
#: Fehlalarm gezählt.
BEWERTETE_TYPEN = frozenset(
    {
        "PERSON",
        "ORGANIZATION",
        "LOCATION",
        "EMAIL_ADDRESS",
        "CH_AHV",
        "CH_IBAN",
        "IBAN_CODE",
        "CH_PHONE",
        "CH_POSTAL_CODE",
        "CH_PLATE",
        # Ebene 0 — kommt ausschliesslich ueber die Begriffsliste. Muss
        # mitbewertet werden, sonst wuerden Erwartungen dieser Typen
        # stillschweigend ignoriert und ein Testfall dafuer waere wirkungslos.
        "PTI_PROJEKT",
        "PTI_PRODUKT",
    }
)

#: Struktur-PII — hier gilt die harte Anforderung von 100 % Recall.
STRUKTUR_PII_TYPEN = frozenset(
    {"CH_AHV", "CH_IBAN", "IBAN_CODE", "EMAIL_ADDRESS", "CH_PHONE"}
)

#: Zielmarke für Personennamen (Fachkonzept §13.1).
MIN_PERSON_RECALL = 0.90

#: Zielmarke für die Gesamt-Precision (Fachkonzept §13.1).
MIN_PRECISION_GESAMT = 0.90


def lade_generator() -> Optional[Any]:
    """Lädt ``golden-set/synthetic/generator.py``, falls es schon existiert.

    Das Verzeichnis ``golden-set`` trägt einen Bindestrich und ist damit kein
    gültiger Python-Bezeichner — ein normales ``import golden_set...`` scheidet
    aus. Es reicht aber, ``golden-set`` selbst auf den Suchpfad zu legen:
    ``synthetic`` darunter hat keinen Bindestrich und lässt sich als
    gewöhnliches (implizites Namespace-)Package importieren.

    Returns
    -------
    module or None
        Das geladene Modul, oder ``None``, wenn die Datei noch nicht
        existiert (das Golden-Set wird parallel gebaut).
    """
    golden_set_dir = REPO_ROOT / "golden-set"
    generator_datei = golden_set_dir / "synthetic" / "generator.py"
    if not generator_datei.exists():
        return None

    pfad = str(golden_set_dir)
    if pfad not in sys.path:
        sys.path.insert(0, pfad)
    return importlib.import_module("synthetic.generator")


# ── Datenmodell der Auswertung ──────────────────────────────────────────────


@dataclass(frozen=True)
class Treffer:
    """Ein erwarteter Fund, der tatsächlich gefunden wurde (TP)."""

    dokument: str
    entity_type: str
    text: str
    score: float
    confidence: str


@dataclass(frozen=True)
class VerfehlteErwartung:
    """Ein erwarteter Fund, der in den Ergebnissen fehlt (FN)."""

    dokument: str
    entity_type: str
    text: str
    grund: str = ""


@dataclass(frozen=True)
class Fehlalarm:
    """Ein Fund, der nicht erwartet war (FP).

    ``ist_verboten`` unterscheidet die zwei Quellen eines Fehlalarms: ein
    ganz gewöhnlicher unerwarteter Fund, oder ein Treffer auf einen
    ``verboten``-Eintrag — Letzteres ist die schwerwiegendere Kategorie, weil
    das Golden-Set diesen Fund explizit ausschliesst (z. B. ein Name, der
    nie als PERSON markiert werden darf).
    """

    dokument: str
    entity_type: str
    text: str
    score: float
    confidence: str
    ist_verboten: bool = False
    grund: str = ""


@dataclass(frozen=True)
class Kennzahlen:
    """Precision, Recall und F1 aus rohen Treffer-/Fehlerzählungen.

    ``erwartet`` (die Grundgesamtheit für Recall) ergibt sich als
    ``treffer + verfehlt`` und wird bewusst nicht separat gespeichert, damit
    beide Zahlen nie auseinanderlaufen können.
    """

    treffer: int
    verfehlt: int
    fehlalarme: int

    @property
    def erwartet(self) -> int:
        """Anzahl erwarteter Funde: Treffer plus Verfehlte."""
        return self.treffer + self.verfehlt

    @property
    def precision(self) -> Optional[float]:
        """Anteil echter Treffer an allen Funden, oder ``None`` ohne Funde."""
        nenner = self.treffer + self.fehlalarme
        return self.treffer / nenner if nenner else None

    @property
    def recall(self) -> Optional[float]:
        """Anteil gefundener an allen erwarteten, oder ``None`` ohne Erwartung."""
        return self.treffer / self.erwartet if self.erwartet else None

    @property
    def f1(self) -> Optional[float]:
        """Harmonisches Mittel aus Precision und Recall, oder ``None``."""
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class DokumentAuswertung:
    """Auswertung eines einzelnen Testdokuments."""

    dokument: str
    treffer: list
    verfehlt: list
    fehlalarme: list


@dataclass(frozen=True)
class Gesamtauswertung:
    """Auswertung über alle ausgewerteten Testdokumente hinweg."""

    dokumente: list

    @property
    def treffer(self) -> list:
        """Alle Treffer über alle Dokumente hinweg."""
        return [t for d in self.dokumente for t in d.treffer]

    @property
    def verfehlt(self) -> list:
        """Alle verfehlten Erwartungen über alle Dokumente hinweg."""
        return [v for d in self.dokumente for v in d.verfehlt]

    @property
    def fehlalarme(self) -> list:
        """Alle Fehlalarme über alle Dokumente hinweg."""
        return [f for d in self.dokumente for f in d.fehlalarme]

    @property
    def verbotene_funde(self) -> list:
        """Die Teilmenge der Fehlalarme, die einen ``verboten``-Eintrag trifft."""
        return [f for f in self.fehlalarme if f.ist_verboten]

    def typen(self) -> list:
        """Alle Entitätstypen, zu denen mindestens ein Datenpunkt existiert."""
        gesehen = (
            {t.entity_type for t in self.treffer}
            | {v.entity_type for v in self.verfehlt}
            | {f.entity_type for f in self.fehlalarme}
        )
        return sorted(gesehen)

    def kennzahlen(self, entity_type: Optional[str] = None) -> Kennzahlen:
        """Precision/Recall/F1, gesamt oder für einen einzelnen Typ.

        Parameters
        ----------
        entity_type
            Auf diesen Typ einschränken. ``None`` liefert die Kennzahlen
            über alle bewerteten Typen hinweg (die Gesamtzeile der Tabelle).

        Returns
        -------
        Kennzahlen
            Treffer-, Verfehlt- und Fehlalarmzahl samt abgeleiteter Raten.
        """
        if entity_type is None:
            treffer, verfehlt, fehlalarme = (
                self.treffer,
                self.verfehlt,
                self.fehlalarme,
            )
        else:
            treffer = [t for t in self.treffer if t.entity_type == entity_type]
            verfehlt = [v for v in self.verfehlt if v.entity_type == entity_type]
            fehlalarme = [
                f for f in self.fehlalarme if f.entity_type == entity_type
            ]
        return Kennzahlen(len(treffer), len(verfehlt), len(fehlalarme))


# ── Erkennung und Auswertung ─────────────────────────────────────────────────


def erkenne_funde(dok: Any) -> list:
    """Führt Adapter-Extraktion und Erkennung für ein Testdokument aus.

    Parameters
    ----------
    dok
        Ein ``Testdokument`` aus dem Golden-Set-Vertrag.

    Returns
    -------
    list of Finding
        Die rohen Funde der Pipeline, unbewertet und ungefiltert.
    """
    adapter = get_adapter(dok.dateiname)
    segmente = adapter.extract_segments(dok.inhalt)
    # Die Begriffsliste (Ebene 0) wird je Dokument mitgegeben. Die meisten
    # Dokumente brauchen keine; wo sie gesetzt ist, prueft der Testfall genau
    # diese Ebene — Behoerden-, Projekt- und Produktnamen, die an nichts
    # ausser dem Listeneintrag erkennbar sind.
    begriffe = list(getattr(dok, "begriffe", ()) or ())
    ergebnis = pipeline.detect_in_segments(segmente, custom_terms=begriffe or None)
    return ergebnis.findings


def werte_dokument_aus(dok: Any) -> DokumentAuswertung:
    """Vergleicht die Funde eines Dokuments mit seinen Erwartungen.

    Der Abgleich läuft über das Paar ``(text, entity_type)`` als Multimenge:
    kommt derselbe Erwartungs-Eintrag mehrfach vor, muss er auch mehrfach
    gefunden werden, damit alle Vorkommen als Treffer zählen.

    Nur Funde, deren Typ in :data:`BEWERTETE_TYPEN` steht, werden
    berücksichtigt — ebenso werden ``erwartet``- und ``verboten``-Einträge
    ausserhalb dieser Menge stillschweigend übersprungen, statt fälschlich
    als dauerhaft verfehlt zu gelten.

    Parameters
    ----------
    dok
        Ein ``Testdokument`` aus dem Golden-Set-Vertrag.

    Returns
    -------
    DokumentAuswertung
        Treffer, verfehlte Erwartungen und Fehlalarme dieses Dokuments.
    """
    bewertete_funde = [
        f for f in erkenne_funde(dok) if f.entity_type in BEWERTETE_TYPEN
    ]

    offene_erwartungen: dict = {}
    for erwartung in dok.erwartet:
        if erwartung.entity_type not in BEWERTETE_TYPEN:
            continue
        schluessel = (erwartung.text, erwartung.entity_type)
        offene_erwartungen.setdefault(schluessel, []).append(erwartung)

    verboten_paare = {
        (v.text, v.entity_type): v
        for v in dok.verboten
        if v.entity_type in BEWERTETE_TYPEN
    }

    treffer: list = []
    fehlalarme: list = []

    for fund in bewertete_funde:
        paar = (fund.text, fund.entity_type)
        warteschlange = offene_erwartungen.get(paar)
        if warteschlange:
            # Ein offener Erwartungs-Eintrag wird "verbraucht" — bei
            # Duplikaten zählt jedes Vorkommen einzeln.
            warteschlange.pop(0)
            treffer.append(
                Treffer(
                    dok.name, fund.entity_type, fund.text, fund.score,
                    fund.confidence,
                )
            )
        elif paar in verboten_paare:
            fehlalarme.append(
                Fehlalarm(
                    dok.name,
                    fund.entity_type,
                    fund.text,
                    fund.score,
                    fund.confidence,
                    ist_verboten=True,
                    grund=verboten_paare[paar].grund,
                )
            )
        else:
            fehlalarme.append(
                Fehlalarm(
                    dok.name, fund.entity_type, fund.text, fund.score,
                    fund.confidence,
                )
            )

    verfehlt = [
        VerfehlteErwartung(dok.name, e.entity_type, e.text, e.grund)
        for reste in offene_erwartungen.values()
        for e in reste
    ]

    return DokumentAuswertung(dok.name, treffer, verfehlt, fehlalarme)


def werte_aus(dokumente: Sequence[Any]) -> Gesamtauswertung:
    """Wertet mehrere Testdokumente aus und fasst sie zusammen.

    Parameters
    ----------
    dokumente
        Die auszuwertenden ``Testdokument``-Instanzen.

    Returns
    -------
    Gesamtauswertung
        Die Auswertung je Dokument, plus aggregierte Kennzahlen.
    """
    return Gesamtauswertung([werte_dokument_aus(d) for d in dokumente])


# ── Ausgabe für Menschen ─────────────────────────────────────────────────────


def _formatiere_rate(wert: Optional[float]) -> str:
    """Formatiert eine Rate als Prozentzahl, oder ``"n/a"`` ohne Datenbasis."""
    return f"{wert:.1%}" if wert is not None else "n/a"


def formatiere_tabelle(auswertung: Gesamtauswertung) -> str:
    """Baut die Kennzahlen-Tabelle je Entitätstyp plus Gesamtzeile.

    Parameters
    ----------
    auswertung
        Das Ergebnis von :func:`werte_aus`.

    Returns
    -------
    str
        Die fertig formatierte, mehrzeilige Tabelle.
    """
    spalten = (
        f"{'Typ':<18}{'erwartet':>9}{'gefunden':>9}{'verfehlt':>9}"
        f"{'fehlalarme':>11}{'precision':>11}{'recall':>9}{'f1':>8}"
    )
    trenner = "-" * len(spalten)

    zeilen = ["Kennzahlen je Entitätstyp", trenner, spalten, trenner]
    for typ in auswertung.typen():
        k = auswertung.kennzahlen(typ)
        zeilen.append(
            f"{typ:<18}{k.erwartet:>9}{k.treffer:>9}{k.verfehlt:>9}"
            f"{k.fehlalarme:>11}{_formatiere_rate(k.precision):>11}"
            f"{_formatiere_rate(k.recall):>9}{_formatiere_rate(k.f1):>8}"
        )

    zeilen.append(trenner)
    gesamt = auswertung.kennzahlen()
    zeilen.append(
        f"{'GESAMT':<18}{gesamt.erwartet:>9}{gesamt.treffer:>9}"
        f"{gesamt.verfehlt:>9}{gesamt.fehlalarme:>11}"
        f"{_formatiere_rate(gesamt.precision):>11}"
        f"{_formatiere_rate(gesamt.recall):>9}{_formatiere_rate(gesamt.f1):>8}"
    )
    return "\n".join(zeilen)


def formatiere_verfehlte(auswertung: Gesamtauswertung) -> str:
    """Listet jede verfehlte Erwartung mit Dokumentname auf.

    Parameters
    ----------
    auswertung
        Das Ergebnis von :func:`werte_aus`.

    Returns
    -------
    str
        Die fertig formatierte, mehrzeilige Liste.
    """
    verfehlt = auswertung.verfehlt
    zeilen = [f"Verfehlte Erwartungen ({len(verfehlt)})", "-" * 70]
    if not verfehlt:
        zeilen.append("  (keine)")
    for v in verfehlt:
        grund = f" — {v.grund}" if v.grund else ""
        zeilen.append(f"  [{v.dokument}] {v.entity_type} {v.text!r}{grund}")
    return "\n".join(zeilen)


def formatiere_fehlalarme(auswertung: Gesamtauswertung) -> str:
    """Listet jeden Fehlalarm mit Dokumentname und Ampelstufe auf.

    Die Ampelstufe (``confidence``) macht sichtbar, ob ein Fehlalarm knapp
    über der Schwelle lag (rot/gelb) oder mit hoher Sicherheit gemeldet wurde
    (grün) — Letzteres ist der dringendere Fall für die Erkenner-Pflege.

    Parameters
    ----------
    auswertung
        Das Ergebnis von :func:`werte_aus`.

    Returns
    -------
    str
        Die fertig formatierte, mehrzeilige Liste.
    """
    fehlalarme = auswertung.fehlalarme
    zeilen = [f"Fehlalarme ({len(fehlalarme)})", "-" * 70]
    if not fehlalarme:
        zeilen.append("  (keine)")
    for f in fehlalarme:
        kennzeichnung = "VERBOTEN" if f.ist_verboten else "unerwartet"
        grund = f" — {f.grund}" if f.grund else ""
        zeilen.append(
            f"  [{f.dokument}] {f.entity_type} {f.text!r} "
            f"(score={f.score:.2f}, ampel={f.confidence}, {kennzeichnung}){grund}"
        )
    return "\n".join(zeilen)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Kommandozeileneinstieg: lädt das Golden-Set und druckt die Auswertung.

    Parameters
    ----------
    argv
        Argumente ohne Programmname, für Tests. ``None`` liest ``sys.argv``.

    Returns
    -------
    int
        Exitcode: ``0`` bei Erfolg, ``1`` wenn das Golden-Set fehlt, leer ist
        oder ``--dokument`` keinen Treffer hat.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Misst die Erkennungsqualität des PTI-Pseudonymisierers am "
            "Golden-Set und gibt Precision/Recall/F1 je Entitätstyp sowie "
            "alle verfehlten Erwartungen und Fehlalarme aus."
        )
    )
    parser.add_argument(
        "--dokument",
        metavar="NAME",
        help="Nur dieses eine Testdokument auswerten (Testdokument.name).",
    )
    args = parser.parse_args(argv)

    generator = lade_generator()
    if generator is None:
        print(
            "golden-set/synthetic/generator.py fehlt — das Golden-Set ist "
            "noch nicht gebaut. Es gibt nichts zu messen.",
            file=sys.stderr,
        )
        return 1

    dokumente = list(generator.DOKUMENTE)
    if args.dokument is not None:
        gefiltert = [d for d in dokumente if d.name == args.dokument]
        if not gefiltert:
            namen = ", ".join(sorted(d.name for d in dokumente)) or "(keine)"
            print(
                f"Kein Testdokument namens {args.dokument!r}. "
                f"Verfügbar: {namen}",
                file=sys.stderr,
            )
            return 1
        dokumente = gefiltert

    if not dokumente:
        print("Keine Testdokumente im Golden-Set.", file=sys.stderr)
        return 1

    auswertung = werte_aus(dokumente)

    print(f"Golden-Set-Auswertung — {len(dokumente)} Dokument(e)\n")
    print(formatiere_tabelle(auswertung))
    print()
    print(formatiere_verfehlte(auswertung))
    print()
    print(formatiere_fehlalarme(auswertung))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
