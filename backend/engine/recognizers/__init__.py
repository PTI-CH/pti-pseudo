"""Schweizspezifische Erkenner und die gepflegte Begriffsliste.

Alle Module hier sind leichtgewichtig — sie ziehen weder spaCy noch ein
Sprachmodell nach sich und lassen sich deshalb einzeln und schnell testen.
"""

from backend.engine.recognizers.ahv import SwissAhvRecognizer
from backend.engine.recognizers.custom_terms import (
    CustomTerm,
    CustomTermsRecognizer,
    MatchMode,
    build_custom_terms_recognizer,
)
from backend.engine.recognizers.iban_ch import SwissIbanRecognizer
from backend.engine.recognizers.phone_ch import SwissPhoneRecognizer
from backend.engine.recognizers.plate_ch import SwissPlateRecognizer
from backend.engine.recognizers.plz_ch import SwissPostalCodeRecognizer

__all__ = [
    "CustomTerm",
    "CustomTermsRecognizer",
    "MatchMode",
    "SwissAhvRecognizer",
    "SwissIbanRecognizer",
    "SwissPhoneRecognizer",
    "SwissPlateRecognizer",
    "SwissPostalCodeRecognizer",
    "build_custom_terms_recognizer",
]
