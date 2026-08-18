"""Tests für den Entitätskatalog und die NLP-Konfiguration.

Beides sind reine Datenstrukturen — die Tests laufen ohne Sprachmodell und
in Millisekunden. Sie sichern die Stellen ab, an denen ein Tippfehler sonst
still ins Leere liefe.
"""

from __future__ import annotations

import pytest

from backend.engine import entities, nlp


# ── Katalog ────────────────────────────────────────────────────────────────


def test_alle_konstanten_stehen_im_katalog():
    """Jede exportierte Konstante muss auch einen Katalogeintrag haben."""
    konstanten = {
        entities.PERSON,
        entities.LOCATION,
        entities.ORGANIZATION,
        entities.EMAIL_ADDRESS,
        entities.IBAN_CODE,
        entities.CREDIT_CARD,
        entities.IP_ADDRESS,
        entities.URL,
        entities.DATE_TIME,
        entities.CH_AHV,
        entities.CH_IBAN,
        entities.CH_PHONE,
        entities.CH_POSTAL_CODE,
        entities.CH_PLATE,
    }
    assert konstanten == set(entities.CATALOG)


def test_katalogschluessel_und_eintrag_stimmen_ueberein():
    for key, definition in entities.CATALOG.items():
        assert key == definition.entity_type


@pytest.mark.parametrize("entity_type", list(entities.CATALOG))
def test_jeder_typ_hat_ein_deutsches_label(entity_type):
    label = entities.get_label(entity_type)
    assert label
    assert label != entity_type  # ein technischer Bezeichner ist kein Label


def test_ch_entities_enthaelt_genau_die_schweizer_typen():
    assert set(entities.CH_ENTITIES) == {
        entities.CH_AHV,
        entities.CH_IBAN,
        entities.CH_PHONE,
        entities.CH_POSTAL_CODE,
        entities.CH_PLATE,
    }


def test_mehrdeutige_typen_haben_default_keep():
    """PLZ und Kontrollschild werden markiert, nicht ungefragt ersetzt."""
    assert entities.get_policy(entities.CH_POSTAL_CODE) is entities.Policy.KEEP
    assert entities.get_policy(entities.CH_PLATE) is entities.Policy.KEEP
    assert set(entities.entities_with_policy(entities.Policy.KEEP)) == {
        entities.CH_POSTAL_CODE,
        entities.CH_PLATE,
    }


def test_eindeutige_typen_haben_default_redact():
    for entity_type in (
        entities.PERSON,
        entities.EMAIL_ADDRESS,
        entities.CH_AHV,
        entities.CH_IBAN,
        entities.CH_PHONE,
    ):
        assert entities.get_policy(entity_type) is entities.Policy.REDACT


def test_unbekannter_typ_faellt_konservativ_zurueck():
    """Im Zweifel schwärzen — und die Oberfläche darf nicht abstürzen."""
    assert entities.get_label("VOELLIG_NEU") == "VOELLIG_NEU"
    assert entities.get_policy("VOELLIG_NEU") is entities.Policy.REDACT


def test_policy_ist_json_tauglich():
    """``Policy`` erbt von ``str`` und passt damit direkt in eine Antwort."""
    assert entities.Policy.KEEP == "keep"
    assert entities.Policy.REDACT == "redact"


def test_definitionen_sind_unveraenderlich():
    definition = entities.CATALOG[entities.PERSON]
    with pytest.raises(Exception):
        definition.label = "Andere"


# ── NLP-Konfiguration ──────────────────────────────────────────────────────


def test_konfiguration_nennt_das_deutsche_modell():
    config = nlp.build_nlp_configuration("de_core_news_md")
    assert config["nlp_engine_name"] == "spacy"
    assert config["models"] == [
        {"lang_code": "de", "model_name": "de_core_news_md"}
    ]


def test_ner_zuordnung_deckt_das_deutsche_schema_ab():
    """Das deutsche Modell liefert PER/LOC/ORG, Presidio erwartet die Langform."""
    mapping = nlp.build_nlp_configuration("de_core_news_md")[
        "ner_model_configuration"
    ]["model_to_presidio_entity_mapping"]
    assert mapping["PER"] == entities.PERSON
    assert mapping["LOC"] == entities.LOCATION
    assert mapping["ORG"] == entities.ORGANIZATION


def test_misc_wird_ignoriert():
    """MISC ist im deutschen Modell ein Sammelbecken ohne PII-Wert."""
    config = nlp.build_nlp_configuration("de_core_news_md")
    assert config["ner_model_configuration"]["labels_to_ignore"] == ["MISC"]


def _config_schicht_vorhanden() -> bool:
    """Gibt es ``backend.config`` schon? Dann hat sie Vorrang vor der Umgebung."""
    try:
        import backend.config  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    _config_schicht_vorhanden(),
    reason="backend.config existiert und hat Vorrang vor der Umgebung.",
)
def test_modellname_kommt_aus_der_umgebung(monkeypatch):
    monkeypatch.setenv("SPACY_MODEL", "de_core_news_lg")
    assert nlp.get_spacy_model_name() == "de_core_news_lg"


@pytest.mark.skipif(
    _config_schicht_vorhanden(),
    reason="backend.config existiert und hat Vorrang vor der Umgebung.",
)
def test_modellname_faellt_auf_den_default_zurueck(monkeypatch):
    monkeypatch.delenv("SPACY_MODEL", raising=False)
    assert nlp.get_spacy_model_name() == nlp.DEFAULT_SPACY_MODEL


def test_konfiguration_ist_eine_kopie():
    """Ein Aufrufer darf die Modul-Konstanten nicht versehentlich umbiegen."""
    config = nlp.build_nlp_configuration("de_core_news_md")
    config["ner_model_configuration"]["labels_to_ignore"].append("PER")
    assert nlp.LABELS_TO_IGNORE == ["MISC"]
