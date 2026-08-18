"""Erkenner für Schweizer und liechtensteinische IBAN.

Aufbau
------
Eine CH-/LI-IBAN ist immer genau 21 Zeichen lang::

    CH93 0076 2011 6238 5295 7
    ^^   ^^   ^^^^^^^^^^^^^^^^
    |    |    └─ 17 alphanumerische Zeichen (BBAN: 5 Ziffern Bankclearing
    |    |       plus 12 Zeichen Kontonummer)
    |    └────── zwei Prüfziffern (Mod-97)
    └─────────── Ländercode CH oder LI

Liechtenstein ist mitgenommen, weil es dieselbe 21-Zeichen-Struktur, denselben
Bankclearing-Raum und in Schweizer Dokumenten praktisch dieselbe Häufigkeit
hat wie eine Nummer aus einem kleinen Kanton.

Mod-97-Prüfung (ISO 7064, Variante 97-10)
-----------------------------------------
1. Die ersten vier Zeichen (Ländercode + Prüfziffern) ans Ende verschieben.
2. Buchstaben durch Zahlen ersetzen: ``A``=10, ``B``=11, …, ``Z``=35.
3. Die entstandene Riesenzahl modulo 97 rechnen — das Ergebnis muss 1 sein.

Verhältnis zu Presidios ``IbanRecognizer``
------------------------------------------
Presidio bringt einen generischen IBAN-Erkenner mit, der ebenfalls anspringt.
Dieser hier ist **spezifischer**: er kennt nur CH/LI, dafür mit passgenauer
Länge, und er liefert den fachlich richtigen Entitätstyp ``CH_IBAN`` statt des
allgemeinen ``IBAN_CODE``. Beide feuern damit auf demselben Span.

Aufgelöst wird das **nicht** hier, sondern in :mod:`backend.engine.merge`:
gleicher Span, gleicher Score (beide validieren über Mod-97 und landen auf
1.0) → die Vorrangliste ``PRIORITY_ENTITIES`` entscheidet zugunsten von
``CH_IBAN``. Den generischen Erkenner deshalb *nicht* auszubauen ist Absicht:
er fängt weiterhin ausländische IBAN, die in Schweizer Dokumenten regelmässig
vorkommen.
"""

from __future__ import annotations

from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizer

from backend.engine.entities import CH_IBAN

#: Ländercodes, die dieser Erkenner abdeckt.
SUPPORTED_COUNTRIES = ("CH", "LI")

#: Gesamtlänge einer CH-/LI-IBAN ohne Trennzeichen.
IBAN_LENGTH = 21

CONTEXT = [
    "IBAN",
    "Konto",
    "Kontonummer",
    "Bankverbindung",
    "Zahlungsverbindung",
    "Einzahlung",
    "Überweisung",
    "Lastschrift",
    "Bankclearing",
]


def _to_digits(value: str) -> str:
    """Buchstaben durch ihren Zahlenwert ersetzen (``A``=10 … ``Z``=35)."""
    return "".join(
        str(ord(character) - 55) if character.isalpha() else character
        for character in value
    )


def normalize_iban(value: str) -> str:
    """Trennzeichen entfernen und in Grossbuchstaben umwandeln."""
    return "".join(value.split()).replace("-", "").upper()


def is_valid_swiss_iban(value: str) -> bool:
    """Vollständige Prüfung: Land, Länge, Zeichenvorrat und Mod-97."""
    iban = normalize_iban(value)

    if len(iban) != IBAN_LENGTH:
        return False
    if not iban.startswith(SUPPORTED_COUNTRIES):
        return False
    if not iban.isalnum():
        return False
    # Stellen 3 und 4 sind die Prüfziffern und müssen numerisch sein.
    if not iban[2:4].isdigit():
        return False

    rearranged = iban[4:] + iban[:4]
    return int(_to_digits(rearranged)) % 97 == 1


class SwissIbanRecognizer(PatternRecognizer):
    """Erkennt CH-/LI-IBAN mit gültiger Mod-97-Prüfsumme.

    Das Muster erlaubt zwischen je zwei Zeichen ein optionales Leerzeichen —
    damit trifft es sowohl die kompakte Schreibweise ``CH9300762011623852957``
    als auch die auf Rechnungen übliche Vierergruppierung
    ``CH93 0076 2011 6238 5295 7`` und alle Zwischenformen.

    Bewusst nur das **Leerzeichen** als Trennzeichen: ein Punkt oder
    Bindestrich mitten in der IBAN kommt praktisch nicht vor, würde aber die
    Fehlerquote deutlich erhöhen, weil das Muster dann über Satzzeichen
    hinweg greifen könnte.

    Die Wortgrenze am Ende sorgt dafür, dass eine zu lange Zeichenkette
    (22 Zeichen und mehr) gar nicht erst als Treffer durchgeht, statt vorne
    abgeschnitten zu werden.
    """

    PATTERNS = [
        Pattern(
            name="CH/LI-IBAN",
            regex=r"\b(?:CH|LI)\d{2}(?:[ ]?[A-Z0-9]){17}\b",
            score=0.5,
        ),
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "de",
        supported_entity: str = CH_IBAN,
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns if patterns else list(self.PATTERNS),
            context=context if context else list(CONTEXT),
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        """Mod-97 prüfen.

        ``True`` → Score 1.0, ``False`` → Treffer verworfen. Eine IBAN mit
        falscher Prüfsumme ist keine IBAN; sie als schwachen Treffer zu melden
        würde die Oberfläche nur mit Rauschen füllen.
        """
        return is_valid_swiss_iban(pattern_text)
