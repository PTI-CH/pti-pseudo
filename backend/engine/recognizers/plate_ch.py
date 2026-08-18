"""Erkenner für Schweizer Kontrollschilder (Autonummern).

Aufbau
------
Ein Schweizer Kontrollschild besteht aus dem zweibuchstabigen Kantonskürzel
und einer bis zu sechsstelligen Nummer::

    ZH 123456
    BE 4711
    AI 1

Das Kürzel ist immer in Grossbuchstaben; das Leerzeichen ist optional, weil
in Fliesstext beides vorkommt (``ZH123456``).

Die Mehrdeutigkeit
------------------
``ZH 8000`` ist genauso gut eine Adresszeile wie ein Kontrollschild — die
Kantonskürzel sind identisch mit den Kantonsabkürzungen, die in jeder
Ortsangabe stehen. Dazu kommen Kollisionen mit Aktenzeichen (``BE 2024``),
Normbezeichnungen und Produktcodes.

Deshalb dieselbe Behandlung wie bei der PLZ:

* **zwingendes Kontext-Gating** — ohne Fahrzeugbezug kein Treffer,
* **niedriger Basisscore (0.3)**,
* **Default-Policy ``keep``** in :mod:`backend.engine.entities` — markieren,
  nicht ungefragt ersetzen.

Zusätzlich wird das Kantonskürzel **gross geschrieben** verlangt. Presidio
kompiliert Muster standardmässig mit ``IGNORECASE``; das wird hier über die
inline begrenzte Flag ``(?-i:…)`` gezielt wieder abgeschaltet. Ohne das würde
jedes ``zh`` und ``so`` im Fliesstext zum Kandidaten — „so 1234 Franken" wäre
ein Solothurner Kontrollschild.
"""

from __future__ import annotations

from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from backend.engine.entities import CH_PLATE
from backend.engine.recognizers._context import DEFAULT_WINDOW, has_context

#: Die 26 gültigen Kantonskürzel. Vollständig und abschliessend — die Menge
#: ändert sich nur bei einer Kantonsgründung.
CANTON_CODES = (
    "AG",
    "AI",
    "AR",
    "BE",
    "BL",
    "BS",
    "FR",
    "GE",
    "GL",
    "GR",
    "JU",
    "LU",
    "NE",
    "NW",
    "OW",
    "SG",
    "SH",
    "SO",
    "SZ",
    "TG",
    "TI",
    "UR",
    "VD",
    "VS",
    "ZG",
    "ZH",
)

#: Basisscore. Wie bei der PLZ bewusst niedrig gehalten.
BASE_SCORE = 0.3

CONTEXT = [
    "Kontrollschild",
    "Kontrollschilder",
    "Nummernschild",
    "Kennzeichen",
    "Autonummer",
    "Fahrzeug",
    "Fahrzeugausweis",
    "Halter",
    "Halterin",
    "Personenwagen",
    "Motorrad",
    "Anhänger",
    "Fahrzeugkontrolle",
    "Strassenverkehrsamt",
    "immatrikuliert",
    "Immatrikulation",
]


def is_valid_plate(value: str) -> bool:
    """Kantonskürzel und Nummernlänge prüfen.

    Die Nummer darf nicht mit einer Null beginnen: Schweizer Kontrollschilder
    werden ohne führende Nullen vergeben, und ``ZG 0815`` wäre damit eher ein
    Aktenzeichen als ein Schild.
    """
    compact = value.replace(" ", "")
    if len(compact) < 3:
        return False

    canton, number = compact[:2], compact[2:]
    if canton not in CANTON_CODES:
        return False
    if not number.isdigit() or not 1 <= len(number) <= 6:
        return False
    return not number.startswith("0")


class SwissPlateRecognizer(PatternRecognizer):
    """Erkennt Schweizer Kontrollschilder — aber nur mit Fahrzeugkontext.

    Wie :class:`~backend.engine.recognizers.plz_ch.SwissPostalCodeRecognizer`
    überschreibt dieser Erkenner :meth:`analyze`, weil Presidio keinen Weg
    anbietet, einen Treffer wegen *fehlenden* Kontexts zu verwerfen.
    """

    #: Kantonskürzel als Alternation, mit abgeschalteter Gross-/Kleinschreib-
    #: Toleranz.
    _CANTON_ALTERNATION = "|".join(CANTON_CODES)

    PATTERNS = [
        Pattern(
            name="CH-Kontrollschild",
            regex=rf"\b(?-i:{_CANTON_ALTERNATION})[ ]?\d{{1,6}}\b",
            score=BASE_SCORE,
        ),
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "de",
        supported_entity: str = CH_PLATE,
        context_window: int = DEFAULT_WINDOW,
    ):
        self.context_window = context_window
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns if patterns else list(self.PATTERNS),
            context=context if context else list(CONTEXT),
            supported_language=supported_language,
        )

    def analyze(
        self,
        text: str,
        entities: List[str],
        nlp_artifacts: Optional[NlpArtifacts] = None,
        regex_flags: Optional[int] = None,
    ) -> List[RecognizerResult]:
        """Muster anwenden und anschliessend hart nach Kontext filtern."""
        results = super().analyze(text, entities, nlp_artifacts, regex_flags)

        kept: List[RecognizerResult] = []
        for result in results:
            candidate = text[result.start : result.end]
            if not is_valid_plate(candidate):
                continue
            if not has_context(
                text, result.start, result.end, self.context, self.context_window
            ):
                continue
            kept.append(result)

        return kept
