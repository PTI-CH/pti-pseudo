"""Erkenner für die Schweizer Sozialversicherungsnummer (AHV/AVS).

Aufbau der Nummer
-----------------
Seit 2008 gilt die 13-stellige Versichertennummer::

    756.XXXX.XXXX.XP
    ^^^ ^^^^^^^^^^ ^
     |       |     └─ Prüfziffer (EAN-13)
     |       └─────── neun Zufallsziffern, ohne jede Bedeutung
     └─────────────── Länderpräfix 756 (Schweiz, nach ISO 3166-1 numerisch)

Der Nummernteil ist bewusst bedeutungsfrei — im Gegensatz zur alten
AHV-Nummer, aus der sich Name, Geburtsdatum und Geschlecht ableiten liessen.

Prüfziffer
----------
Berechnet wie bei einem EAN-13-Strichcode: die ersten zwölf Ziffern werden
abwechselnd mit 1 und 3 gewichtet (Position 1 hat Gewicht 1), die Summe
gebildet und die Prüfziffer als ``(10 − Summe mod 10) mod 10`` bestimmt.

Warum das wichtig ist: die Prüfziffer verwandelt ein sehr grobes Muster
(``756`` plus zehn beliebige Ziffern) in einen praktisch fehlerfreien
Erkenner. Nur eine von zehn zufälligen Ziffernfolgen besteht sie. Deshalb
liefert dieser Erkenner bei bestandener Prüfung volle Konfidenz — und bei
gescheiterter Prüfung **gar keinen** Treffer statt eines schwachen.

Presidio-Mechanik
-----------------
``PatternRecognizer.validate_result`` steuert das direkt:
``True`` hebt den Score auf ``EntityRecognizer.MAX_SCORE`` (1.0), ``False``
setzt ihn auf ``MIN_SCORE`` (0.0), und Treffer mit Score 0 werden vom
Basis-Erkenner verworfen. Der Score der Muster selbst ist damit nur ein
Zwischenwert und nie das Endergebnis.
"""

from __future__ import annotations

from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizer

from backend.engine.entities import CH_AHV

#: Länderpräfix jeder gültigen AHV-Nummer.
AHV_PREFIX = "756"

#: Kontextwörter. Sie ändern am Score nichts mehr (der steht nach der
#: Prüfziffernprüfung bereits fest), sind aber für die Erklärungsanzeige und
#: für einen späteren Wechsel auf ein score-basiertes Verfahren gepflegt.
CONTEXT = [
    "AHV",
    "AHV-Nr",
    "AHV-Nr.",
    "AHV-Nummer",
    "AHVN13",
    "AVS",
    "Sozialversicherungsnummer",
    "Versichertennummer",
    "Versicherten-Nr",
]


def compute_check_digit(first_twelve: str) -> int:
    """Prüfziffer nach EAN-13 zu den ersten zwölf Ziffern berechnen.

    :param first_twelve: Genau zwölf Ziffern als String.
    :return: Die Prüfziffer als ``int`` zwischen 0 und 9.
    :raises ValueError: Wenn die Eingabe nicht aus genau zwölf Ziffern besteht.

    Gewichtung: Position 1 → 1, Position 2 → 3, Position 3 → 1, … Der Index in
    Python ist nullbasiert, deshalb steht das Gewicht 1 bei geradem Index.
    """
    if len(first_twelve) != 12 or not first_twelve.isdigit():
        raise ValueError("Für die EAN-13-Prüfziffer braucht es genau zwölf Ziffern.")

    total = sum(
        int(digit) * (1 if index % 2 == 0 else 3)
        for index, digit in enumerate(first_twelve)
    )
    return (10 - total % 10) % 10


def is_valid_ahv(number: str) -> bool:
    """Prüfen, ob ``number`` eine gültige AHV-Nummer ist.

    Trennzeichen (Punkte, Leerzeichen, Bindestriche) werden vorher entfernt,
    damit ``756.1234.5678.97`` und ``7561234567897`` identisch behandelt
    werden.
    """
    digits = "".join(character for character in number if character.isdigit())

    if len(digits) != 13:
        return False
    if not digits.startswith(AHV_PREFIX):
        return False

    return int(digits[12]) == compute_check_digit(digits[:12])


def format_ahv(digits: str) -> str:
    """13 Ziffern in die Schreibweise ``756.XXXX.XXXX.XX`` bringen.

    Hilfsfunktion für Tests und für die Anzeige; die Erkennung braucht sie
    nicht.
    """
    digits = "".join(character for character in digits if character.isdigit())
    if len(digits) != 13:
        raise ValueError("Eine AHV-Nummer hat genau 13 Ziffern.")
    return f"{digits[:3]}.{digits[3:7]}.{digits[7:11]}.{digits[11:]}"


class SwissAhvRecognizer(PatternRecognizer):
    r"""Erkennt Schweizer AHV-Nummern mit gültiger EAN-13-Prüfziffer.

    Zwei Muster, weil beide Schreibweisen in freier Wildbahn vorkommen:

    * **punktiert** — ``756.1234.5678.97``, die amtliche Darstellung.
    * **kompakt** — ``7561234567897``, wie sie aus CSV-Exporten und
      Schnittstellen fällt.

    Beide Muster tragen einen Zwischenscore; entschieden wird ausschliesslich
    über die Prüfziffer in :meth:`validate_result`.

    Der Wortgrenzen-Anker ``\b`` verhindert, dass die kompakte Variante
    mitten aus einer längeren Ziffernkette geschnitten wird — ohne ihn wäre
    jede 20-stellige Referenznummer ein Kandidat.
    """

    PATTERNS = [
        Pattern(
            name="AHV punktiert (756.XXXX.XXXX.XX)",
            regex=r"\b756\.\d{4}\.\d{4}\.\d{2}\b",
            score=0.6,
        ),
        Pattern(
            name="AHV kompakt (13 Ziffern)",
            regex=r"\b756\d{10}\b",
            score=0.4,
        ),
        Pattern(
            name="AHV mit Leerzeichen (756 XXXX XXXX XX)",
            regex=r"\b756 \d{4} \d{4} \d{2}\b",
            score=0.5,
        ),
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "de",
        supported_entity: str = CH_AHV,
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns if patterns else list(self.PATTERNS),
            context=context if context else list(CONTEXT),
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        """Prüfziffer verifizieren.

        ``True`` → Score 1.0, ``False`` → Treffer wird verworfen. Ein
        ``None`` gibt es hier bewusst nicht: bei einer AHV-Nummer ist die
        Prüfziffer immer entscheidbar, ein „vielleicht" wäre unehrlich.
        """
        return is_valid_ahv(pattern_text)
