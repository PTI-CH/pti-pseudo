"""Erkenner für Schweizer Telefonnummern.

Nummernraum
-----------
Eine Schweizer Rufnummer hat national immer **neun Ziffern** (die sogenannte
national signifikante Nummer, NSN), geschrieben mit vorangestellter
Verkehrsausscheidungsziffer ``0``::

    044 123 45 67        national,      0 + 9 Ziffern
    +41 44 123 45 67     international, +41 + 9 Ziffern
    0041 44 123 45 67    internationale Wählform mit 00

Die ersten Ziffern der NSN sind die Gebietskennzahl. Sie zu prüfen ist der
eigentliche Wert dieses Erkenners: ohne diese Prüfung wäre jede
zehnstellige Ziffernfolge, die mit 0 beginnt, eine Telefonnummer — also auch
jede Bestell-, Referenz- und Belegnummer.

Trennzeichen
------------
Erlaubt sind Leerzeichen, Punkt, Schrägstrich und Bindestrich, an beliebiger
Stelle zwischen den Ziffern. Damit sind alle üblichen Gruppierungen
abgedeckt (``044/123 45 67``, ``+41-44-123-45-67``, ``044.123.45.67``), ohne
dass jede Variante einzeln aufgezählt werden müsste. Zusätzlich toleriert das
Muster die auf Briefköpfen verbreitete Form ``+41 (0)44 123 45 67``.

Konfidenz
---------
Wie bei AHV und IBAN entscheidet eine Validierung, nicht der Musterscore:
gültige Gebietskennzahl → Score 1.0, sonst kein Treffer. Eine Nummer, die
Form *und* Nummernraum erfüllt, ist keine Vermutung mehr.
"""

from __future__ import annotations

from typing import List, Optional

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer

from backend.engine.entities import CH_PHONE

#: Zweistellige Gebietskennzahlen des Festnetzes (BAKOM-Nummerierungsplan).
#: 21–27 Westschweiz, 31–34 Bern/Solothurn, 41–44 Zentralschweiz und Zürich,
#: 51 SBB, 52 Winterthur/Schaffhausen, 55–58 Ostschweiz und Sondernetze,
#: 61–62 Basel/Aargau, 71 St. Gallen, 81 Graubünden, 91 Tessin.
AREA_CODES = frozenset(
    {
        "21",
        "22",
        "23",
        "24",
        "25",
        "26",
        "27",
        "31",
        "32",
        "33",
        "34",
        "41",
        "42",
        "43",
        "44",
        "51",
        "52",
        "55",
        "56",
        "57",
        "58",
        "61",
        "62",
        "71",
        "81",
        "91",
    }
)

#: Mobilfunkbereiche.
MOBILE_CODES = frozenset({"74", "75", "76", "77", "78", "79"})

#: Dreistellige Dienstenummern: Gratis- (0800/0840er) und Mehrwertdienste.
#: Sie brechen das zweistellige Schema und werden deshalb getrennt geprüft.
SERVICE_CODES = frozenset({"800", "840", "842", "844", "848", "900", "901", "906"})

CONTEXT = [
    "Telefon",
    "Tel",
    "Tel.",
    "Telefonnummer",
    "Mobile",
    "Natel",
    "Handy",
    "Festnetz",
    "Fax",
    "Rufnummer",
    "erreichbar",
]

#: Alles ausser Ziffern wird für die Prüfung entfernt.
_NON_DIGIT = re.compile(r"\D")


def to_national_number(value: str) -> Optional[str]:
    """Beliebige Schreibweise auf die neunstellige NSN zurückführen.

    :return: Neun Ziffern, oder ``None``, wenn die Ziffernzahl nicht passt.

    Die Reihenfolge der Fälle ist wichtig: ``0041`` muss vor ``41`` und vor
    ``0`` geprüft werden, sonst würde die führende Doppelnull als
    Verkehrsausscheidungsziffer missverstanden.

    Der letzte Schritt fängt die Briefkopfform ``+41 (0)44 …`` ab: nach dem
    Entfernen der Klammern bleibt eine Null zu viel stehen, die als
    nationale Verkehrsziffer wegfällt.
    """
    digits = _NON_DIGIT.sub("", value)

    if digits.startswith("0041"):
        national = digits[4:]
    elif digits.startswith("41"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    # +41 (0)44 … → nach dem Ländercode steht noch die nationale Null.
    if len(national) == 10 and national.startswith("0"):
        national = national[1:]

    return national if len(national) == 9 else None


def is_valid_swiss_phone(value: str) -> bool:
    """Prüfen, ob eine gültige Schweizer Rufnummer vorliegt.

    Geprüft wird die Gebietskennzahl: erst die dreistelligen Dienstenummern
    (``0800``, ``0848``, ``0900`` …), dann die zweistelligen Festnetz- und
    Mobilbereiche. Die Reihenfolge spielt hier keine Rolle, weil sich die
    Mengen nicht überschneiden — ``80``, ``84`` und ``90`` sind bewusst
    *keine* gültigen zweistelligen Bereiche.
    """
    national = to_national_number(value)
    if national is None:
        return False

    if national[:3] in SERVICE_CODES:
        return True
    return national[:2] in AREA_CODES | MOBILE_CODES


class SwissPhoneRecognizer(PatternRecognizer):
    r"""Erkennt Schweizer Festnetz-, Mobil- und Dienstnummern.

    Das Muster ist bewusst grosszügig und die Präzision kommt aus
    :meth:`validate_result`. Der umgekehrte Weg — alle gültigen Vorwahlen ins
    Regex zu schreiben — wäre eine unleserliche Alternation von über
    dreissig Zweigen, die bei jeder Änderung des Nummerierungsplans neu
    gebaut werden müsste.

    Die Lookbehind-Bedingung ``(?<![\d+])`` verhindert, dass das Muster
    mitten in einer längeren Ziffernkette ansetzt; ``(?!\d)`` am Ende
    verhindert, dass es zu früh aufhört und die Restziffern liegen lässt.
    """

    PATTERNS = [
        Pattern(
            name="CH-Telefon (international)",
            regex=(
                r"(?<![\d+])(?:\+41|0041)"
                r"(?:[ ./\-]?\(0\))?"
                r"(?:[ ./\-]?\d){9}(?!\d)"
            ),
            score=0.7,
        ),
        Pattern(
            name="CH-Telefon (national)",
            regex=r"(?<![\d+])0(?:[ ./\-]?\d){9}(?!\d)",
            score=0.5,
        ),
        Pattern(
            name="CH-Telefon (Ländercode ohne Plus)",
            regex=r"(?<![\d+])41(?:[ ./\-]?\d){9}(?!\d)",
            score=0.4,
        ),
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "de",
        supported_entity: str = CH_PHONE,
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns if patterns else list(self.PATTERNS),
            context=context if context else list(CONTEXT),
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        """Gebietskennzahl prüfen; ``False`` verwirft den Treffer."""
        return is_valid_swiss_phone(pattern_text)
