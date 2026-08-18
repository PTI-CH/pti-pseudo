"""Aufbau der spaCy-NLP-Engine für Presidio mit deutschem Modell.

Warum ein Modul-Singleton?
--------------------------
``de_core_news_md`` ist rund 400 MB im Arbeitsspeicher und braucht beim
erstmaligen Laden mehrere Sekunden (gemessen: 7.1 s kalt). Das darf **nie**
pro Request passieren. Deshalb liegt die geladene Engine in einem
Modul-globalen Cache, den ``get_nlp_engine()`` befüllt.

In Produktion läuft gunicorn mit ``preload_app=True``: die Engine wird einmal
im Master-Prozess erzeugt und über ``fork`` an die Worker vererbt. Der Cache
hier ist die Voraussetzung dafür, dass das überhaupt greift — ohne ihn würde
jeder Worker beim ersten Request erneut laden und der erste Aufruf liefe in
den Gateway-Timeout.

Der Cache ist nach Modellnamen geschlüsselt, damit ein Test mit einem anderen
Modell den Produktionscache nicht überschreibt.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

from presidio_analyzer.nlp_engine import NlpEngine, NlpEngineProvider

logger = logging.getLogger("pti.engine.nlp")

#: Sprachcode, den die gesamte Engine verwendet. Presidio schleift ihn durch
#: alle Erkenner durch; ein abweichender Code führt dazu, dass Erkenner
#: stillschweigend nicht greifen.
LANGUAGE = "de"

#: Fallback, falls weder ``backend.config`` noch die Umgebung etwas sagen.
#: Muss zum Modell-Wheel in ``pti-requirements.txt`` passen.
DEFAULT_SPACY_MODEL = "de_core_news_md"

#: Übersetzung der spaCy-NER-Labels auf Presidio-Entitätstypen.
#: Das deutsche Modell liefert ``PER``/``LOC``/``ORG``/``MISC`` (Tübingen-
#: Schema), Presidio erwartet ``PERSON``/``LOCATION``/``ORGANIZATION``.
#: ``PERSON`` ist zusätzlich aufgeführt, weil neuere spaCy-Modelle das Label
#: bereits ausgeschrieben liefern — die Zuordnung soll beides überleben.
MODEL_TO_PRESIDIO_ENTITY_MAPPING: Dict[str, str] = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "LOC": "LOCATION",
    "ORG": "ORGANIZATION",
    "MISC": "MISC",
}

#: ``MISC`` ist im deutschen Modell ein Sammelbecken für alles, was das NER
#: nicht einordnen kann (Produktnamen, Ereignisse, Nationalitäten). Als
#: PII-Signal ist das wertlos und erzeugt nur Rauschen, deshalb verworfen.
LABELS_TO_IGNORE = ["MISC"]

#: Entitäten, die grundsätzlich mit reduzierter Konfidenz geliefert werden.
#: Bewusst leer: die Score-Steuerung findet bei uns in den Erkennern und in
#: ``merge.py`` statt, nicht schon in der NLP-Schicht.
LOW_SCORE_ENTITY_NAMES: list = []

# ── Singleton-Verwaltung ───────────────────────────────────────────────────
# Der Lock schützt gegen den Fall, dass zwei Threads gleichzeitig den kalten
# Cache treffen und beide das Modell laden — das wären kurzzeitig 800 MB.
_ENGINE_CACHE: Dict[str, NlpEngine] = {}
_ENGINE_LOCK = threading.Lock()


def get_spacy_model_name() -> str:
    """Namen des zu ladenden spaCy-Modells ermitteln.

    Reihenfolge der Quellen:

    1. ``backend.config.get_settings().spacy_model`` — die reguläre Quelle,
       sobald die Konfigurationsschicht existiert.
    2. Umgebungsvariable ``SPACY_MODEL`` — dieselbe Variable, aus der die
       Konfigurationsschicht ihren Wert zieht (siehe ``.env.example``).
       Damit läuft die Engine auch isoliert, etwa im Test.
    3. :data:`DEFAULT_SPACY_MODEL`.

    Der Import von ``backend.config`` erfolgt bewusst *innerhalb* der Funktion
    und mit weitem ``except``: die Engine soll nicht davon abhängen, dass die
    Konfigurationsschicht schon gebaut ist, und ein Fehler dort darf hier
    nicht als ImportError beim Modulimport hochschlagen.
    """
    try:
        from backend.config import get_settings  # type: ignore[import-not-found]

        model_name = getattr(get_settings(), "spacy_model", None)
        if model_name:
            return str(model_name)
    except Exception:  # noqa: BLE001 — Konfigurationsschicht ist optional
        logger.debug(
            "backend.config nicht verfügbar, Modellname kommt aus der Umgebung."
        )

    return os.environ.get("SPACY_MODEL") or DEFAULT_SPACY_MODEL


def build_nlp_configuration(model_name: Optional[str] = None) -> Dict[str, Any]:
    """Presidio-NLP-Konfiguration für das deutsche spaCy-Modell erzeugen.

    Als eigene Funktion herausgezogen, damit die Konfiguration im Test
    geprüft werden kann, ohne das 400-MB-Modell zu laden.
    """
    model_name = model_name or get_spacy_model_name()
    return {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": LANGUAGE, "model_name": model_name}],
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": dict(MODEL_TO_PRESIDIO_ENTITY_MAPPING),
            "low_score_entity_names": list(LOW_SCORE_ENTITY_NAMES),
            "labels_to_ignore": list(LABELS_TO_IGNORE),
        },
    }


def create_nlp_engine(model_name: Optional[str] = None) -> NlpEngine:
    """Eine **neue**, ungecachte NLP-Engine bauen und laden.

    Direkt aufrufen sollte das niemand ausser :func:`get_nlp_engine` — jeder
    Aufruf kostet die volle Ladezeit und den vollen Speicher.
    """
    model_name = model_name or get_spacy_model_name()
    logger.info("Lade spaCy-Modell %s (das dauert einige Sekunden) …", model_name)
    provider = NlpEngineProvider(nlp_configuration=build_nlp_configuration(model_name))
    engine = provider.create_engine()
    logger.info("spaCy-Modell %s geladen.", model_name)
    return engine


def get_nlp_engine(model_name: Optional[str] = None) -> NlpEngine:
    """Die geladene NLP-Engine liefern.

    Beim ersten Aufruf wird geladen, danach kommt sie aus dem Cache.

    Doppelt geprüftes Sperren: der günstige Cache-Treffer läuft ohne Lock, nur
    der kalte Pfad serialisiert.
    """
    model_name = model_name or get_spacy_model_name()

    engine = _ENGINE_CACHE.get(model_name)
    if engine is not None:
        return engine

    with _ENGINE_LOCK:
        # Zweite Prüfung: ein anderer Thread kann geladen haben, während
        # dieser hier auf den Lock gewartet hat.
        engine = _ENGINE_CACHE.get(model_name)
        if engine is None:
            engine = create_nlp_engine(model_name)
            _ENGINE_CACHE[model_name] = engine
        return engine


def is_loaded(model_name: Optional[str] = None) -> bool:
    """Prüfen, ob das Modell bereits im Cache liegt (ohne es zu laden)."""
    return (model_name or get_spacy_model_name()) in _ENGINE_CACHE


def reset_nlp_engine() -> None:
    """Cache leeren.

    Nur für Tests und für ein bewusstes Neuladen nach Konfigurationswechsel.
    Der Speicher wird erst frei, wenn auch die letzte ``AnalyzerEngine``, die
    die Engine referenziert, verworfen ist.
    """
    with _ENGINE_LOCK:
        _ENGINE_CACHE.clear()


def warmup() -> NlpEngine:
    """Modell vorab laden — für den gunicorn-Master mit ``preload_app=True``.

    Bewusst ein sprechender Name, damit im Startskript sichtbar ist, *warum*
    dort ein scheinbar wirkungsloser Aufruf steht.
    """
    return get_nlp_engine()
