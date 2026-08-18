r"""Erkenner für Schweizer Postleitzahlen — die Präzisionsfalle.

Das Problem
-----------
Eine Schweizer PLZ ist vierstellig und liegt zwischen 1000 und 9999. Das
naheliegende Muster ``\b[1-9]\d{3}\b`` trifft damit exakt denselben
Zeichenvorrat wie

* jede Jahreszahl seit 1000 — ``im Jahr 2026``,
* jeder vierstellige Betrag — ``CHF 8500``,
* jedes Aktenzeichen und jede Artikelnummer — ``Artikel 1234``,
* jede Hausnummer, Seitenzahl, Bestellnummer, Zimmernummer.

Ein Erkenner, der all das als Personendatum meldet, ist schlimmer als keiner:
er ertränkt die echten Treffer in Rauschen und erzieht die Nutzer dazu,
Vorschläge pauschal wegzuklicken.

Die Lösung: drei Schranken
--------------------------
1. **Zwingendes Kontext-Gating.** Ohne ein Adress-Kontextwort im Fenster um
   die Fundstelle gibt es *keinen* Treffer — nicht etwa einen schwachen.
   Presidios eingebauter Kontextmechanismus kann das nicht leisten, weil er
   Scores nur anhebt und nie unterdrückt; deshalb die harte Prüfung in
   :meth:`SwissPostalCodeRecognizer.analyze`. Die ``context``-Liste ist
   trotzdem gesetzt: sie hebt anschliessend den Score der Überlebenden über
   Presidios ``LemmaContextAwareEnhancer``.
2. **Negativfilter direkt links.** Steht unmittelbar vor der Zahl ein
   Währungs-, Jahres- oder Aktenzeichen-Marker, ist es keine PLZ — auch wenn
   zufällig ein Adresswort in der Nähe steht.
3. **Niedriger Basisscore (0.35).** Selbst ein bestätigter Treffer bleibt eine
   Vermutung. Zusammen mit der Default-Policy ``keep`` in
   :mod:`backend.engine.entities` heisst das: anzeigen, markieren, aber nicht
   ungefragt ersetzen.

Nicht implementiert: die Prüfung gegen das amtliche PLZ-Verzeichnis der Post.
Sie würde die Präzision weiter heben, bringt aber eine pflegebedürftige
Datei mit rund 4000 Einträgen mit. Die Bereichsprüfung 1000–9999 fängt den
grössten Teil davon zum Nulltarif.
"""

from __future__ import annotations

from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from backend.engine.entities import CH_POSTAL_CODE
from backend.engine.recognizers._context import (
    DEFAULT_WINDOW,
    has_context,
    preceded_by,
)

#: Kleinste und grösste real vergebene Schweizer PLZ.
MIN_POSTAL_CODE = 1000
MAX_POSTAL_CODE = 9999

#: Höchster Score, den dieser Erkenner selbst vergibt. Bewusst unter 0.4 —
#: über Presidios Kontext-Enhancer kann er auf rund 0.7 steigen, bleibt damit
#: aber immer noch unter jedem prüfsummenvalidierten Treffer.
BASE_SCORE = 0.35

#: Adress-Kontextwörter. Sie sind gleichzeitig Presidio-``context`` und
#: Eingangstor des harten Gates. Nur rechte Wortgrenze (siehe ``_context``),
#: deshalb trifft ``strasse`` auch ``Bahnhofstrasse`` und ``adresse`` auch
#: ``Zustelladresse``.
CONTEXT = [
    "PLZ",
    "Postleitzahl",
    "wohnhaft",
    "Adresse",
    "Wohnort",
    "Domizil",
    "c/o",
    "Strasse",
    "Zustelladresse",
    "Anschrift",
    "Postfach",
    "Wohnsitz",
    # Bewusst *nicht* das blosse „Ort": ohne linke Wortgrenze träfe es auch
    # „Antwort" und „Standort" und liesse damit halbe Fliesstexte durch.
    "gasse",
    "weg",
    "platz",
]

#: Marker, die unmittelbar links vor der Zahl stehen können und sie eindeutig
#: als Nicht-PLZ ausweisen.
NEGATIVE_PREFIXES = [
    "CHF",
    "Fr.",
    "EUR",
    "€",
    "USD",
    "$",
    "Jahr",
    "Jahre",
    "seit",
    "Artikel",
    "Art.",
    "Ziff.",
    "Ziffer",
    "Absatz",
    "Abs.",
    "Seite",
    "Nr.",
    "Rechnung",
    "Betrag",
]


def is_plausible_postal_code(value: str) -> bool:
    """Prüfen, ob die Zahl im Bereich real vergebener CH-Postleitzahlen liegt."""
    digits = value.strip()
    if not digits.isdigit() or len(digits) != 4:
        return False
    return MIN_POSTAL_CODE <= int(digits) <= MAX_POSTAL_CODE


class SwissPostalCodeRecognizer(PatternRecognizer):
    """Erkennt vierstellige Schweizer PLZ — aber nur mit Adresskontext.

    Der Erkenner überschreibt :meth:`analyze`, weil Presidio keinen Haken für
    „Treffer verwerfen, wenn der Kontext fehlt" anbietet.
    ``validate_result``/``invalidate_result`` sehen nur den Treffertext selbst,
    nicht seine Umgebung — für die Frage, ob ``8500`` ein Betrag oder eine PLZ
    ist, ist genau die Umgebung aber das einzige Signal.
    """

    PATTERNS = [
        Pattern(
            name="CH-PLZ (vierstellig)",
            regex=r"\b[1-9]\d{3}\b",
            score=BASE_SCORE,
        ),
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "de",
        supported_entity: str = CH_POSTAL_CODE,
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

            # Schranke 3: Bereichsprüfung. Das Regex garantiert bereits
            # 1000–9999; die Prüfung steht hier trotzdem, damit ein
            # nachträglich gelockertes Muster nicht still die Grenze reisst.
            if not is_plausible_postal_code(candidate):
                continue

            # Schranke 2: Währung, Jahr, Aktenzeichen direkt davor.
            if preceded_by(text, result.start, NEGATIVE_PREFIXES):
                continue

            # Schranke 1: ohne Adresskontext kein Treffer.
            if not has_context(
                text, result.start, result.end, self.context, self.context_window
            ):
                continue

            kept.append(result)

        return kept
