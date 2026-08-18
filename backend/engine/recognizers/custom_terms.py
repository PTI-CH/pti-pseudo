"""Layer 0 des Fachkonzepts: die gepflegte Begriffsliste.

Wozu
----
Kein statistisches Modell und keine Heuristik kennt die internen Bezeichnungen
eines Hauses: Projektnamen, Deckbezeichnungen, Aktenkürzel, Systemnamen. Genau
diese Begriffe sind aber oft das, was ein Dokument identifizierbar macht.
Deshalb steht vor allen anderen Erkennern eine **ausdrücklich gepflegte
Liste**, deren Treffer mit Score 1.0 gemeldet werden.

Score 1.0 ist keine Prahlerei, sondern die Aussage: *ein Mensch hat diesen
Begriff eingetragen*. Eine so gepflegte Liste schlägt jede Heuristik, und über
:mod:`backend.engine.merge` verdrängt sie deshalb zuverlässig jeden
konkurrierenden Treffer auf demselben Textstück.

Kein Datenbankzugriff
---------------------
Die Liste wird **injiziert**, nicht geladen. Dieses Modul kennt weder
SQLAlchemy noch eine Session; es bekommt eine Sequenz von :class:`CustomTerm`
und baut daraus einen Erkenner. Damit ist es ohne laufende Datenbank testbar,
und die spätere Persistenzschicht kann die Begriffe beliebig beziehen
(Tabelle, YAML-Datei, Request-Parameter).

Zwei Abgleichmodi
-----------------
``exact``
    Wortgrenzengenau und **gross-/kleinschreibungsempfindlich**. Für Kürzel,
    bei denen die Schreibweise trägt: ``KKPKS`` soll treffen, ``kkpks`` in
    ``kkpks.example.com`` nicht.
``normalized``
    Gross-/Kleinschreibung egal, Umlaute in beiden Schreibweisen
    (``Zürich`` ↔ ``Zuerich``), Leerraum beliebig (auch über einen
    Zeilenumbruch hinweg). Für Klarnamen und mehrwortige Bezeichnungen, die in
    Dokumenten unterschiedlich getippt werden.

Warum keine Textnormalisierung
------------------------------
Naheliegend wäre, für ``normalized`` den ganzen Text zu falten und darin zu
suchen. Das geht nicht: ``ü → ue`` verändert die Zeichenlängen, und alle
Fundstellen dahinter lägen um die Differenz verschoben. Presidio-Ergebnisse
sind aber Offsets in den **Originaltext** — verschobene Offsets ersetzen im
Ergebnisdokument die falschen Zeichen. Deshalb wird stattdessen das *Muster*
toleranter gemacht: jeder Umlaut wird zu einer Alternative, die beide
Schreibweisen abdeckt, und der Text bleibt unangetastet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import regex as re
from presidio_analyzer import (
    AnalysisExplanation,
    EntityRecognizer,
    LocalRecognizer,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpArtifacts

logger = logging.getLogger("pti.engine.custom_terms")

#: Entitätstyp, den Begriffe ohne eigene Angabe erhalten. Bewusst nicht im
#: Katalog von :mod:`backend.engine.entities`: welche Typen eine Installation
#: pflegt, entscheidet die Installation.
DEFAULT_ENTITY = "CUSTOM_TERM"

#: Konfidenz jedes Listentreffers.
TERM_SCORE = 1.0


class MatchMode(str, Enum):
    """Abgleichmodus eines Begriffs."""

    EXACT = "exact"
    NORMALIZED = "normalized"


@dataclass(frozen=True)
class CustomTerm:
    """Ein Eintrag der Begriffsliste.

    :param term: Der zu suchende Begriff.
    :param entity_type: Entitätstyp, unter dem der Treffer gemeldet wird.
    :param match_mode: ``exact`` oder ``normalized``.
    """

    term: str
    entity_type: str = DEFAULT_ENTITY
    match_mode: MatchMode = MatchMode.EXACT

    def __post_init__(self) -> None:
        """Eingaben prüfen und ``match_mode`` aus einem String übernehmen."""
        if not self.term or not self.term.strip():
            raise ValueError("Ein Begriff darf nicht leer sein.")
        # ``frozen=True`` verbietet die direkte Zuweisung, deshalb der Umweg
        # über ``object.__setattr__`` — der übliche Weg für Normalisierung in
        # eingefrorenen Dataclasses.
        object.__setattr__(self, "match_mode", MatchMode(self.match_mode))


#: Alles, was als Begriffsliste hereingereicht werden darf.
TermInput = Union[
    "CustomTerm",
    Tuple[str, str, Union[str, MatchMode]],
    Tuple[str, str],
    Tuple[str],
    str,
    Dict[str, str],
]

#: Umlaut-Alternativen für den Modus ``normalized``. Beide Richtungen, damit
#: sowohl der Begriff als auch der Fundtext in jeder Schreibweise stehen darf.
_UMLAUT_ALTERNATIVES = {
    "ä": "(?:ä|ae)",
    "ö": "(?:ö|oe)",
    "ü": "(?:ü|ue)",
    "ß": "(?:ß|ss)",
}

#: Zweibuchstabige Ersatzschreibweisen, die ebenfalls beide Formen abdecken
#: sollen. ``ss`` ist absichtlich dabei, damit ``Strasse`` auch ``Straße``
#: trifft.
_DIGRAPH_ALTERNATIVES = {
    "ae": "(?:ä|ae)",
    "oe": "(?:ö|oe)",
    "ue": "(?:ü|ue)",
    "ss": "(?:ß|ss)",
}


def coerce_term(item: TermInput) -> CustomTerm:
    """Beliebige Eingabeform in einen :class:`CustomTerm` überführen.

    Erlaubt sind ``CustomTerm``, ein Tupel ``(begriff, entity_type,
    match_mode)`` (die letzten beiden optional), ein blosser String und ein
    Dictionary mit denselben Schlüsseln. Die Bequemlichkeit ist Absicht: die
    Liste kommt später aus einer Datenbankzeile, aus JSON oder aus einem Test
    — und soll überall gleich aussehen dürfen.
    """
    if isinstance(item, CustomTerm):
        return item
    if isinstance(item, str):
        return CustomTerm(term=item)
    if isinstance(item, dict):
        return CustomTerm(
            term=item["term"],
            entity_type=item.get("entity_type", DEFAULT_ENTITY),
            match_mode=item.get("match_mode", MatchMode.EXACT),
        )
    if isinstance(item, (tuple, list)):
        values = list(item)
        term = values[0]
        entity_type = values[1] if len(values) > 1 else DEFAULT_ENTITY
        match_mode = values[2] if len(values) > 2 else MatchMode.EXACT
        return CustomTerm(term=term, entity_type=entity_type, match_mode=match_mode)

    raise TypeError(f"Unbekannte Form eines Begriffs: {item!r}")


def _exact_body(term: str) -> str:
    """Regex-Rumpf für den Modus ``exact``.

    ``(?-i:…)`` schaltet die von Presidio global gesetzte
    ``IGNORECASE``-Flag für genau diesen Abschnitt ab — nur so ist der Modus
    seinem Namen gerecht.
    """
    return f"(?-i:{re.escape(term)})"


def _normalized_body(term: str) -> str:
    r"""Regex-Rumpf für den Modus ``normalized``.

    Aufgebaut Zeichen für Zeichen:

    * Leerraum → ``\s+`` (ein Umbruch mitten im Begriff bricht nichts),
    * Umlaut oder passender Digraph → Alternative beider Schreibweisen,
    * alles andere → maskiertes Literal.

    ``(?i:…)`` erzwingt die Toleranz gegenüber Gross-/Kleinschreibung
    unabhängig davon, mit welchen Flags Presidio das Muster kompiliert.
    """
    lowered = term.lower()
    parts: List[str] = []
    index = 0
    while index < len(lowered):
        character = lowered[index]

        if character.isspace():
            # Ganzen Leerraumblock zusammenfassen.
            while index < len(lowered) and lowered[index].isspace():
                index += 1
            parts.append(r"\s+")
            continue

        digraph = lowered[index : index + 2]
        if digraph in _DIGRAPH_ALTERNATIVES:
            parts.append(_DIGRAPH_ALTERNATIVES[digraph])
            index += 2
            continue

        if character in _UMLAUT_ALTERNATIVES:
            parts.append(_UMLAUT_ALTERNATIVES[character])
            index += 1
            continue

        parts.append(re.escape(character))
        index += 1

    return "(?i:" + "".join(parts) + ")"


def build_term_regex(term: CustomTerm) -> str:
    r"""Vollständiges Regex eines Begriffs, inklusive Wortgrenzen.

    Statt ``\b`` wird ``(?:^|(?<=\W))…(?:(?=\W)|$)`` verwendet — dasselbe
    Konstrukt, das auch Presidios Deny-List benutzt. Grund: ``\b`` versagt,
    sobald ein Begriff mit einem Nicht-Wortzeichen beginnt oder endet
    (``§14a``, ``C++``, ``Projekt-X.``). Die Lookaround-Variante prüft die
    Umgebung statt des Übergangs und funktioniert in allen Fällen.
    """
    body = (
        _normalized_body(term.term)
        if term.match_mode is MatchMode.NORMALIZED
        else _exact_body(term.term)
    )
    return r"(?:^|(?<=\W))" + body + r"(?:(?=\W)|$)"


class CustomTermsRecognizer(LocalRecognizer):
    """Erkenner über eine zur Laufzeit injizierte Begriffsliste.

    Bewusst ein eigener :class:`LocalRecognizer` und kein
    ``PatternRecognizer``: letzterer trägt genau **einen** Entitätstyp, die
    Begriffsliste soll aber gemischt sein dürfen (ein Projektname als
    ``ORGANIZATION``, ein Deckname als ``PERSON``, ein Systemname als
    ``CUSTOM_TERM``).

    Die Muster werden einmal in :meth:`load` kompiliert. Bei einer
    Listenänderung wird ein neuer Erkenner gebaut, nicht der bestehende
    verändert — so kann ein laufender Request nie über einer halb
    ausgetauschten Liste stolpern.
    """

    def __init__(
        self,
        terms: Optional[Iterable[TermInput]] = None,
        supported_language: str = "de",
        name: str = "CustomTermsRecognizer",
        score: float = TERM_SCORE,
    ):
        self.terms: List[CustomTerm] = [coerce_term(item) for item in (terms or [])]
        self.score = score
        self._compiled: List[Tuple[CustomTerm, "re.Pattern[str]"]] = []

        # Ohne Begriffe gibt es keine Entitätstypen. Presidio braucht aber eine
        # nicht-leere Liste, sonst wird der Erkenner nie aufgerufen und der
        # Registry-Eintrag ist wertlos — deshalb der Platzhalter.
        entity_types = sorted({term.entity_type for term in self.terms}) or [
            DEFAULT_ENTITY
        ]

        super().__init__(
            supported_entities=entity_types,
            name=name,
            supported_language=supported_language,
        )

    def load(self) -> None:
        """Alle Begriffe zu Regex kompilieren.

        Wird von ``EntityRecognizer.__init__`` aufgerufen. Ein fehlerhafter
        Begriff darf die gesamte Liste nicht sprengen: er wird protokolliert
        und übersprungen.
        """
        self._compiled = []
        for term in self.terms:
            try:
                self._compiled.append(
                    (term, re.compile(build_term_regex(term), flags=re.MULTILINE))
                )
            except re.error:  # pragma: no cover - nur bei kaputtem Begriff
                logger.warning(
                    "Begriff %r konnte nicht kompiliert werden und wird übersprungen.",
                    term.term,
                )

    def analyze(
        self,
        text: str,
        entities: List[str],
        nlp_artifacts: Optional[NlpArtifacts] = None,
    ) -> List[RecognizerResult]:
        """Text gegen alle Begriffe prüfen.

        ``entities`` filtert wie üblich: fragt der Aufrufer nur nach
        bestimmten Typen, werden Begriffe anderer Typen nicht ausgewertet.
        """
        results: List[RecognizerResult] = []

        for term, pattern in self._compiled:
            if entities and term.entity_type not in entities:
                continue

            for match in pattern.finditer(text):
                start, end = match.span()
                explanation = AnalysisExplanation(
                    recognizer=self.name,
                    original_score=self.score,
                    pattern_name=f"Begriffsliste ({term.match_mode.value})",
                    pattern=pattern.pattern,
                    validation_result=None,
                    textual_explanation=(
                        f"Treffer aus der gepflegten Begriffsliste: {term.term!r}"
                    ),
                )
                results.append(
                    RecognizerResult(
                        entity_type=term.entity_type,
                        start=start,
                        end=end,
                        score=self.score,
                        analysis_explanation=explanation,
                        recognition_metadata={
                            RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                            RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                        },
                    )
                )

        # Zwei Begriffe können denselben Span treffen (etwa „PTI" und
        # „PTI Schweiz"). Presidios Standardhelfer räumt identische Treffer
        # weg; die restlichen Überlappungen löst ``merge_overlaps`` auf.
        return EntityRecognizer.remove_duplicates(results)


def build_custom_terms_recognizer(
    terms: Optional[Sequence[TermInput]] = None,
    supported_language: str = "de",
) -> CustomTermsRecognizer:
    """Bequemer Bauaufruf für :class:`CustomTermsRecognizer`.

    Existiert, damit :mod:`backend.engine.analyzer` die Liste durchreichen
    kann, ohne die Klasse zu importieren.
    """
    return CustomTermsRecognizer(terms=terms, supported_language=supported_language)
