"""Zusammenbau der Presidio-``AnalyzerEngine`` für den PTI-Pseudonymisierer.

Was hier zusammenkommt
----------------------
1. Die deutsche spaCy-NLP-Engine aus :mod:`backend.engine.nlp` (Modul-Singleton,
   siehe dort — das Modell wird nie pro Request geladen).
2. Presidios vordefinierte Erkenner für ``de``: E-Mail, IBAN, IP, URL,
   Kreditkarte, Datum, Telefon, Krypto, MAC, UUID sowie der
   ``SpacyRecognizer``, der ``PERSON``/``LOCATION``/``ORGANIZATION`` aus dem
   NER-Modell zieht.
3. Die schweizspezifischen Erkenner aus
   :mod:`backend.engine.recognizers` — AHV, CH/LI-IBAN, Telefon, PLZ,
   Kontrollschild.
4. Optional die gepflegte Begriffsliste (Layer 0 des Fachkonzepts).

Warum die generischen Erkenner drin bleiben
-------------------------------------------
Unsere CH-Erkenner sind spezifischer, aber nicht vollständiger: eine deutsche
IBAN, eine französische Telefonnummer oder eine Kreditkarte kommen in
Schweizer Dokumenten regelmässig vor. Beide Ebenen laufen deshalb parallel,
und die dabei zwangsläufig entstehenden Doppeltreffer räumt
:func:`backend.engine.merge.merge_overlaps` auf. Das ist billiger und
robuster, als die Zuständigkeiten vorab sauber schneiden zu wollen.

Zwischenspeicherung
-------------------
Der Aufbau der Registry ist nicht teuer, die NLP-Engine dagegen schon. Weil
letztere ohnehin aus dem Modul-Cache kommt, wird zusätzlich die
Standard-``AnalyzerEngine`` (ohne Begriffsliste) einmal gebaut und behalten —
das spart pro Request das Kompilieren sämtlicher Regex-Muster.

Eine Engine **mit** Begriffsliste wird nie zwischengespeichert: die Liste ist
mandanten- und dokumentabhängig, ein Cache darüber wäre ein Datenleck.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, List, Optional, Sequence

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, RecognizerResult
from presidio_analyzer.predefined_recognizers import CreditCardRecognizer

from backend.engine import entities as entity_catalog
from backend.engine.merge import merge_overlaps
from backend.engine.nlp import LANGUAGE, get_nlp_engine
from backend.engine.recognizers.ahv import SwissAhvRecognizer
from backend.engine.recognizers.custom_terms import (
    CustomTermsRecognizer,
    TermInput,
    build_custom_terms_recognizer,
    coerce_term,
)
from backend.engine.recognizers.iban_ch import SwissIbanRecognizer
from backend.engine.recognizers.phone_ch import SwissPhoneRecognizer
from backend.engine.recognizers.plate_ch import SwissPlateRecognizer
from backend.engine.recognizers.plz_ch import SwissPostalCodeRecognizer

logger = logging.getLogger("pti.engine.analyzer")

#: Deutsche Kontextwörter für Presidios Kreditkartenerkenner. Der bringt
#: Kontextlisten nur für en/es/it/pl mit; ohne diesen Eintrag liefe er für
#: ``de`` ohne Kontextunterstützung.
CREDIT_CARD_CONTEXT_DE = [
    "Kreditkarte",
    "Karte",
    "Kartennummer",
    "Visa",
    "Mastercard",
    "Amex",
    "American Express",
    "Diners",
    "Maestro",
]

_DEFAULT_ANALYZER: Optional[AnalyzerEngine] = None
_ANALYZER_LOCK = threading.Lock()


def build_swiss_recognizers(
    supported_language: str = LANGUAGE,
) -> List:
    """Alle schweizspezifischen Erkenner in einer Liste.

    Als eigene Funktion herausgezogen, damit Tests die Erkenner einzeln oder
    als Gruppe gegen eine minimale Engine hängen können, ohne das spaCy-Modell
    zu laden.
    """
    return [
        SwissAhvRecognizer(supported_language=supported_language),
        SwissIbanRecognizer(supported_language=supported_language),
        SwissPhoneRecognizer(supported_language=supported_language),
        SwissPostalCodeRecognizer(supported_language=supported_language),
        SwissPlateRecognizer(supported_language=supported_language),
    ]


def build_registry(
    nlp_engine=None,
    custom_terms: Optional[Sequence[TermInput]] = None,
    supported_language: str = LANGUAGE,
) -> RecognizerRegistry:
    """Registry mit vordefinierten, schweizspezifischen und eigenen Erkennern.

    :param nlp_engine: Geladene NLP-Engine. Ohne sie fehlt der
        ``SpacyRecognizer`` und damit ``PERSON``/``LOCATION``/``ORGANIZATION``.
    :param custom_terms: Optionale Begriffsliste (Layer 0).
    :param supported_language: Sprachcode; im Betrieb immer ``de``.
    """
    registry = RecognizerRegistry(supported_languages=[supported_language])

    # Schritt 1: Presidios Standardsatz für die Sprache.
    registry.load_predefined_recognizers(
        languages=[supported_language], nlp_engine=nlp_engine
    )

    # Schritt 2: Kreditkarte nachrüsten. Presidios YAML-Konfiguration listet
    # für den Kreditkartenerkenner nur en/es/it/pl auf, weshalb er für ``de``
    # gar nicht erst geladen wird — CREDIT_CARD steht aber in unserem Katalog.
    registry.add_recognizer(
        CreditCardRecognizer(
            supported_language=supported_language,
            context=list(CREDIT_CARD_CONTEXT_DE),
        )
    )

    # Schritt 3: die Schweizer Erkenner.
    for recognizer in build_swiss_recognizers(supported_language):
        registry.add_recognizer(recognizer)

    # Schritt 4: die gepflegte Begriffsliste. Nur anhängen, wenn sie auch
    # Einträge hat — ein leerer Erkenner kostet bei jedem Request einen
    # Funktionsaufruf ohne jeden Nutzen.
    if custom_terms:
        registry.add_recognizer(
            build_custom_terms_recognizer(
                custom_terms, supported_language=supported_language
            )
        )

    return registry


def build_analyzer(
    custom_terms: Optional[Sequence[TermInput]] = None,
    supported_language: str = LANGUAGE,
    default_score_threshold: float = 0.0,
) -> AnalyzerEngine:
    """Eine vollständige ``AnalyzerEngine`` bauen.

    :param custom_terms: Begriffsliste als Sequenz von :class:`CustomTerm`,
        Tupeln ``(begriff, entity_type, match_mode)``, Dicts oder Strings.
    :param supported_language: Sprachcode.
    :param default_score_threshold: Untergrenze, unterhalb derer Presidio
        Treffer verwirft. Bewusst 0.0 — gefiltert wird bei uns bewusst
        *nach* dem Merge, damit ein schwacher Treffer einen starken nicht
        stillschweigend verdrängen kann.
    :return: Die fertige Engine.

    Die NLP-Engine kommt aus dem Modul-Singleton; dieser Aufruf lädt das
    spaCy-Modell also nur beim allerersten Mal.
    """
    nlp_engine = get_nlp_engine()
    registry = build_registry(
        nlp_engine=nlp_engine,
        custom_terms=custom_terms,
        supported_language=supported_language,
    )
    return AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=[supported_language],
        default_score_threshold=default_score_threshold,
    )


def get_default_analyzer() -> AnalyzerEngine:
    """Die zwischengespeicherte Engine ohne Begriffsliste.

    Doppelt geprüftes Sperren wie in :mod:`backend.engine.nlp` — der teure
    Aufbau soll auch bei parallelen Erstanfragen nur einmal laufen.
    """
    global _DEFAULT_ANALYZER

    if _DEFAULT_ANALYZER is not None:
        return _DEFAULT_ANALYZER

    with _ANALYZER_LOCK:
        if _DEFAULT_ANALYZER is None:
            logger.info("Baue die Standard-AnalyzerEngine …")
            _DEFAULT_ANALYZER = build_analyzer()
        return _DEFAULT_ANALYZER


def reset_default_analyzer() -> None:
    """Zwischengespeicherte Engine verwerfen. Nur für Tests."""
    global _DEFAULT_ANALYZER
    with _ANALYZER_LOCK:
        _DEFAULT_ANALYZER = None


def analyze(
    text: str,
    language: str = LANGUAGE,
    entities: Optional[Iterable[str]] = None,
    score_threshold: Optional[float] = None,
    custom_terms: Optional[Sequence[TermInput]] = None,
    analyzer: Optional[AnalyzerEngine] = None,
    merge: bool = True,
) -> List[RecognizerResult]:
    """Text analysieren und die Treffer überlappungsfrei zurückgeben.

    :param text: Der zu analysierende Text.
    :param language: Sprachcode, im Betrieb ``de``.
    :param entities: Optionale Einschränkung auf bestimmte Entitätstypen.
    :param score_threshold: Optionale Untergrenze. Wird **vor** dem Merge
        angewendet, weil ein Treffer unterhalb der Schwelle gar nicht erst
        um einen Span mitbieten soll.
    :param custom_terms: Begriffsliste. Ist sie gesetzt, wird eine eigene,
        nicht zwischengespeicherte Engine gebaut.
    :param analyzer: Vorgefertigte Engine — überschreibt ``custom_terms``.
        Gedacht für Tests und für Aufrufer, die die Engine selbst verwalten.
    :param merge: Auf ``False`` setzen, um die rohen Presidio-Treffer zu
        sehen. Nur für Diagnose; im Betrieb immer ``True``.
    :return: Treffer, nach Textposition sortiert und ohne Überlappungen.

    Der Unterschied zu einem nackten ``AnalyzerEngine.analyze`` ist genau
    dieser letzte Schritt: Presidio liefert pro Erkenner unabhängige Treffer,
    die sich auf demselben Textstück stapeln. Ohne
    :func:`~backend.engine.merge.merge_overlaps` würde eine E-Mail-Adresse als
    E-Mail, als Person und in zwei Bruchstücken als URL gemeldet.
    """
    if analyzer is None:
        analyzer = (
            build_analyzer(custom_terms=custom_terms)
            if custom_terms
            else get_default_analyzer()
        )

    results = analyzer.analyze(
        text=text,
        language=language,
        entities=list(entities) if entities else None,
        score_threshold=score_threshold,
    )

    return merge_overlaps(results) if merge else list(results)


def supported_entities(analyzer: Optional[AnalyzerEngine] = None) -> List[str]:
    """Alle Entitätstypen, die die Engine tatsächlich liefern kann.

    Nützlich für die Oberfläche: der Katalog in
    :mod:`backend.engine.entities` beschreibt, was wir *anzeigen* wollen,
    diese Funktion sagt, was die Engine *kann*.
    """
    analyzer = analyzer or get_default_analyzer()
    return sorted(set(analyzer.get_supported_entities(language=LANGUAGE)))


def servable_entities(
    requested: Iterable[str],
    custom_terms: Optional[Sequence[TermInput]] = None,
    analyzer: Optional[AnalyzerEngine] = None,
) -> List[str]:
    """Die Teilmenge von ``requested``, die dieser Aufruf tatsächlich bedienen kann.

    Der Katalog in :mod:`backend.engine.entities` und die Menge der
    **registrierten Erkenner** sind nicht dasselbe. Am deutlichsten bei den
    ``PTI_``-Typen: sie stehen im Katalog, aber kein Erkenner findet sie von
    selbst — sie entstehen ausschliesslich über die Begriffsliste, und der
    :class:`CustomTermsRecognizer` leitet seine ``supported_entities`` aus den
    tatsächlich eingetragenen Begriffen ab. Ohne einen Begriff vom Typ
    ``PTI_PROJEKT`` gibt es für ``PTI_PROJEKT`` also keinen Erkenner.

    Presidio quittiert eine Anfrage, für die es keinen einzigen zuständigen
    Erkenner findet, mit ``ValueError`` — für die Aufruferin ein 500, obwohl
    sie einen gültigen Katalogtyp geschickt hat. Deshalb wird die Wunschliste
    vorher hier geschnitten; bleibt nichts übrig, hat der Aufruf schlicht
    keinen Befund.

    :param requested: Gewünschte Entitätstypen (bereits gegen den Katalog
        geprüft).
    :param custom_terms: Begriffsliste dieses Aufrufs — ihre Typen kommen zur
        bedienbaren Menge hinzu.
    :param analyzer: Vorgefertigte Engine; ohne sie die Standard-Engine.
    :return: ``requested`` in unveränderter Reihenfolge, ohne die Typen, für
        die es keinen Erkenner gibt.
    """
    bedienbar = set(supported_entities(analyzer))
    for item in custom_terms or []:
        bedienbar.add(coerce_term(item).entity_type)
    return [name for name in requested if name in bedienbar]


__all__ = [
    "CustomTermsRecognizer",
    "analyze",
    "build_analyzer",
    "build_registry",
    "build_swiss_recognizers",
    "entity_catalog",
    "get_default_analyzer",
    "reset_default_analyzer",
    "servable_entities",
    "supported_entities",
]
