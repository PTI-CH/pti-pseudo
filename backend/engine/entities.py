"""Zentraler Katalog aller Entitätstypen des PTI-Pseudonymisierers.

Diese Datei ist die einzige Wahrheit darüber, *welche* Entitätstypen die Engine
kennt, wie sie in der Oberfläche heissen und was standardmässig mit ihnen
geschieht. Sowohl die Erkenner (``backend/engine/recognizers/``) als auch die
spätere Anonymisierungs-Schicht beziehen ihre Konstanten von hier — damit ein
Tippfehler wie ``"CH_POSTALCODE"`` sofort auffällt statt still ins Leere zu
laufen.

Zwei Gruppen:

* **Presidio-Standardtypen** — von den eingebauten Erkennern bzw. vom
  spaCy-NER-Modell geliefert (``PERSON``, ``LOCATION``, …).
* **CH_-Typen** — die schweizspezifischen Ergänzungen dieses Projekts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List

# ── Presidio-Standardtypen, die wir tatsächlich nutzen ─────────────────────
# Bewusst nur diese Untermenge: Presidio kennt deutlich mehr Typen (CRYPTO,
# MEDICAL_LICENSE, UUID, MAC_ADDRESS …). Was hier nicht steht, wird von der
# Engine zwar erkannt, aber nicht als Default-Zielmenge angeboten.
PERSON = "PERSON"
LOCATION = "LOCATION"
ORGANIZATION = "ORGANIZATION"
EMAIL_ADDRESS = "EMAIL_ADDRESS"
IBAN_CODE = "IBAN_CODE"
CREDIT_CARD = "CREDIT_CARD"
IP_ADDRESS = "IP_ADDRESS"
URL = "URL"
DATE_TIME = "DATE_TIME"

# ── Schweizspezifische Typen dieses Projekts ───────────────────────────────
CH_AHV = "CH_AHV"
CH_IBAN = "CH_IBAN"
CH_PHONE = "CH_PHONE"
CH_POSTAL_CODE = "CH_POSTAL_CODE"
CH_PLATE = "CH_PLATE"


class Policy(str, Enum):
    """Was mit einem Treffer standardmässig geschieht.

    ``REDACT``
        Der Treffer wird pseudonymisiert. Das ist der Normalfall.
    ``KEEP``
        Der Treffer wird nur *markiert* und im Text belassen. Gedacht für
        mehrdeutige Typen, bei denen ein automatisches Ersetzen mehr kaputt
        macht als es schützt (siehe ``CH_POSTAL_CODE`` und ``CH_PLATE``).

    Erbt von ``str``, damit der Wert ohne Umweg in JSON-Antworten und in
    Vergleiche mit rohen Strings passt.
    """

    REDACT = "redact"
    KEEP = "keep"


@dataclass(frozen=True)
class EntityDefinition:
    """Stammdaten zu genau einem Entitätstyp.

    :param entity_type: Technischer Bezeichner, wie ihn Presidio führt.
    :param label: Deutsches Anzeigelabel für die Oberfläche.
    :param policy: Default-Verhalten der Anonymisierung.
    :param description: Kurze fachliche Erläuterung, u. a. für Tooltips.
    """

    entity_type: str
    label: str
    policy: Policy
    description: str = ""


#: Vollständiger Katalog. Reihenfolge = sinnvolle Anzeigereihenfolge in der GUI.
CATALOG: Dict[str, EntityDefinition] = {
    definition.entity_type: definition
    for definition in (
        EntityDefinition(
            PERSON,
            "Person",
            Policy.REDACT,
            "Namen natürlicher Personen (spaCy-NER, deutsches Modell).",
        ),
        EntityDefinition(
            LOCATION,
            "Ort",
            Policy.REDACT,
            "Orts-, Länder- und Flurnamen.",
        ),
        EntityDefinition(
            ORGANIZATION,
            "Organisation",
            Policy.REDACT,
            "Firmen, Behörden, Vereine.",
        ),
        EntityDefinition(
            EMAIL_ADDRESS,
            "E-Mail-Adresse",
            Policy.REDACT,
            "Vollständige E-Mail-Adressen.",
        ),
        EntityDefinition(
            IBAN_CODE,
            "IBAN (generisch)",
            Policy.REDACT,
            "IBAN beliebiger Länder — der generische Presidio-Erkenner.",
        ),
        EntityDefinition(
            CREDIT_CARD,
            "Kreditkartennummer",
            Policy.REDACT,
            "Kartennummern mit gültiger Luhn-Prüfsumme.",
        ),
        EntityDefinition(
            IP_ADDRESS,
            "IP-Adresse",
            Policy.REDACT,
            "IPv4- und IPv6-Adressen.",
        ),
        EntityDefinition(
            URL,
            "Web-Adresse",
            Policy.REDACT,
            "URLs und Hostnamen.",
        ),
        EntityDefinition(
            DATE_TIME,
            "Datum/Zeit",
            Policy.REDACT,
            "Datums- und Zeitangaben.",
        ),
        EntityDefinition(
            CH_AHV,
            "AHV-Nummer",
            Policy.REDACT,
            "Schweizer Sozialversicherungsnummer 756.XXXX.XXXX.XX "
            "mit EAN-13-Prüfziffer.",
        ),
        EntityDefinition(
            CH_IBAN,
            "IBAN (CH/LI)",
            Policy.REDACT,
            "Schweizer bzw. liechtensteinische IBAN mit Mod-97-Prüfung.",
        ),
        EntityDefinition(
            CH_PHONE,
            "Telefonnummer (CH)",
            Policy.REDACT,
            "Schweizer Festnetz-, Mobil- und Dienstnummern.",
        ),
        EntityDefinition(
            CH_POSTAL_CODE,
            "Postleitzahl (CH)",
            # Default KEEP: eine vierstellige Zahl ist ohne Kontext nicht von
            # Jahreszahl, Betrag oder Aktenzeichen zu unterscheiden. Ein
            # automatisches Ersetzen würde Dokumente still verfälschen, deshalb
            # wird die PLZ nur markiert und die Entscheidung dem Menschen
            # überlassen.
            Policy.KEEP,
            "Vierstellige CH-PLZ — nur mit Adresskontext erkannt, mehrdeutig.",
        ),
        EntityDefinition(
            CH_PLATE,
            "Kontrollschild (CH)",
            # Default KEEP aus demselben Grund: "ZH 8000" ist genauso gut eine
            # Adresszeile wie ein Kontrollschild.
            Policy.KEEP,
            "Kantonskürzel plus Nummer — mehrdeutig gegenüber Adressangaben.",
        ),
    )
}

#: Alle bekannten Entitätstypen in Katalogreihenfolge.
ALL_ENTITIES: List[str] = list(CATALOG)

#: Nur die schweizspezifischen Typen.
CH_ENTITIES: List[str] = [name for name in CATALOG if name.startswith("CH_")]


def get_label(entity_type: str) -> str:
    """Deutsches Anzeigelabel zu einem Entitätstyp.

    Unbekannte Typen (etwa weil ein Presidio-Update einen neuen Erkenner
    mitbringt) liefern den technischen Bezeichner zurück, statt zu scheitern —
    die Oberfläche soll an einem unbekannten Typ nicht zerbrechen.
    """
    definition = CATALOG.get(entity_type)
    return definition.label if definition else entity_type


def get_policy(entity_type: str) -> Policy:
    """Default-Policy zu einem Entitätstyp.

    Unbekannte Typen werden konservativ als ``REDACT`` behandelt: im Zweifel
    lieber zu viel schwärzen als zu wenig.
    """
    definition = CATALOG.get(entity_type)
    return definition.policy if definition else Policy.REDACT


def entities_with_policy(policy: Policy) -> List[str]:
    """Alle Entitätstypen mit der angegebenen Default-Policy."""
    return [name for name, item in CATALOG.items() if item.policy is policy]
