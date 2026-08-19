"""Prueft die ``entities``-Filterung ueber die REST-Schnittstelle.

Deckt den Katalog-Endpunkt (``GET /api/v1/entities``) sowie die Weitergabe der
Kategorienwahl an ``documents:detect``, ``:anonymize`` und ``:anonymize-file``
ab. Die Fixture ist bewusst aus ``test_integration_api.py`` kopiert statt
importiert — jede Testdatei soll unabhaengig lesbar bleiben, ohne stille
Kopplung an eine andere Datei ueber deren Modulgrenze hinweg.
"""

from __future__ import annotations

import base64
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Testtext mit Name, IBAN und Telefonnummer. Frei erfunden — es gehoeren
#: keine echten Personendaten in dieses Repository.
TEXT = (
    "Frau Petra Müller (IBAN CH93 0076 2011 6238 5295 7, Tel. 044 123 45 67) "
    "wohnt in 8001 Zürich."
)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Faehrt die Anwendung hoch, ohne die globale Umgebung zu verschmutzen.

    Konstruktion identisch zu ``test_integration_api.py``: Umgebungsvariablen
    per ``MonkeyPatch`` gesetzt und danach zurueckgenommen, damit der Cache von
    ``get_settings`` nicht ueber Testdateien hinweg falsch weiterlebt.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "b" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()
        reset_engine_cache()


def test_katalog_endpunkt_liefert_alle_typen(client: TestClient) -> None:
    r = client.get("/api/v1/entities")
    assert r.status_code == 200
    typen = {e["entity_type"]: e for e in r.json()["entities"]}
    assert "PERSON" in typen and "CH_IBAN" in typen and "PTI_PROJEKT" in typen
    assert typen["CH_POSTAL_CODE"]["default_policy"] == "keep"
    assert typen["PTI_PROJEKT"]["source"] == "custom_terms"
    assert typen["PERSON"]["label"] == "Person"


def test_detect_mit_entities_liefert_nur_gewaehlte_typen(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:detect", json={"text": TEXT, "entities": ["CH_IBAN"]}
    )
    assert r.status_code == 200, r.text
    typen = {e["entity_type"] for e in r.json()["entities"]}
    assert typen == {"CH_IBAN"}, typen


def test_anonymize_mit_entities_ersetzt_nur_gewaehlte_typen(
    client: TestClient,
) -> None:
    r = client.post(
        "/api/v1/documents:anonymize",
        json={"text": TEXT, "mode": "anonymous", "entities": ["CH_IBAN"]},
    )
    assert r.status_code == 200, r.text
    assert "Petra Müller" in r.json()["text"]  # Name bleibt
    assert "CH93" not in r.json()["text"]  # IBAN ersetzt


def test_explizite_wahl_ersetzt_auch_keep_typen(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize",
        json={
            "text": "Sie wohnt an der Seestrasse 5, 8001 Zürich.",
            "mode": "anonymous",
            "entities": ["CH_POSTAL_CODE"],
        },
    )
    assert r.status_code == 200, r.text
    assert "8001" not in r.json()["text"]


def test_ohne_entities_bleiben_keep_typen_stehen(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize",
        json={
            "text": "Sie wohnt an der Seestrasse 5, 8001 Zürich.",
            "mode": "anonymous",
        },
    )
    assert "8001" in r.json()["text"]


def test_unbekannter_typ_gibt_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:detect", json={"text": TEXT, "entities": ["FOO"]}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "FOO" in detail
    # Die Fehlermeldung muss auch die erlaubten Typen nennen, sonst weiss die
    # Aufruferin nicht, was stattdessen gueltig gewesen waere.
    assert "PERSON" in detail and "CH_IBAN" in detail


def test_leere_liste_gibt_400(client: TestClient) -> None:
    r = client.post("/api/v1/documents:detect", json={"text": TEXT, "entities": []})
    assert r.status_code == 400
    assert "Mindestens eine Kategorie" in r.json()["detail"]


def test_datei_mit_entities_als_kommaliste(client: TestClient) -> None:
    files = {"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")}
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files=files,
        data={"mode": "anonymous", "entities": "CH_IBAN, CH_PHONE"},
    )
    assert r.status_code == 200, r.text
    out = base64.b64decode(r.json()["content_base64"]).decode("utf-8")
    assert "Petra Müller" in out and "CH93" not in out


# ── Katalogtypen ohne aktiven Erkenner ─────────────────────────────────────
#
# ``PTI_PROJEKT``/``PTI_PRODUKT`` stehen im Katalog, aber kein Erkenner findet
# sie von selbst: sie entstehen ausschliesslich ueber die Begriffsliste, und
# der ``CustomTermsRecognizer`` leitet seine unterstuetzten Typen aus den
# tatsaechlich eingetragenen Begriffen ab. Ist die Liste leer, gibt es fuer
# diese Typen keinen einzigen zustaendigen Erkenner — Presidio quittierte das
# vorher mit einem ``ValueError`` und die Aufruferin sah ein 500 auf eine
# voellig gueltige Kategorienwahl.


def test_nur_pti_typ_ohne_begriffe_ist_leerer_befund_statt_500(
    client: TestClient,
) -> None:
    """Der eigentliche Regressionsfall: 200 mit leerem Befund, kein 500."""
    r = client.post(
        "/api/v1/documents:detect", json={"text": TEXT, "entities": ["PTI_PROJEKT"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["entities"] == []
    # Die Nutzungserfassung braucht die Zeichenzahl trotzdem.
    assert r.json()["char_count"] == len(TEXT)


def test_nur_pti_typ_ohne_begriffe_laesst_den_text_unveraendert(
    client: TestClient,
) -> None:
    """Gegenprobe auf dem Ersetzungspfad — nichts erkannt, nichts angefasst."""
    r = client.post(
        "/api/v1/documents:anonymize",
        json={"text": TEXT, "mode": "anonymous", "entities": ["PTI_PRODUKT"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entities"] == []
    assert body["applied"] == 0
    assert body["text"] == TEXT


def test_pti_typ_neben_person_schneidet_nur_den_pti_typ_weg(
    client: TestClient,
) -> None:
    """Der Schnitt darf nur die unbedienbaren Typen treffen, nicht den Rest.

    Ohne diesen Test waere auch ein "bei ``PTI_*`` gleich gar nichts suchen"
    gruen — und die Kategorienwahl der Oberflaeche, in der die PTI-Typen
    mitangehakt sind, faende dann nie mehr eine Person.
    """
    r = client.post(
        "/api/v1/documents:detect",
        json={"text": TEXT, "entities": ["PTI_PROJEKT", "PERSON"]},
    )
    assert r.status_code == 200, r.text
    assert "PERSON" in {e["entity_type"] for e in r.json()["entities"]}


def test_mit_begriff_wird_der_pti_typ_wieder_bedienbar(client: TestClient) -> None:
    """Sobald ein Begriff dieses Typs existiert, gibt es auch einen Erkenner.

    Damit ist gezeigt, dass der Schnitt die Begriffsliste beruecksichtigt und
    nicht bloss alle ``PTI_``-Typen pauschal verwirft.

    Der Begriff wird am Ende wieder entfernt: die ``client``-Fixture ist
    modulweit, ein liegengebliebener Eintrag faerbte die Nachbartests ein.
    """
    angelegt = client.post(
        "/api/v1/terms",
        json={
            "term": "Nordwind",
            "entity_type": "PTI_PROJEKT",
            "match_mode": "normalized",
        },
    )
    assert angelegt.status_code == 201, angelegt.text
    try:
        r = client.post(
            "/api/v1/documents:detect",
            json={
                "text": "Das Projekt Nordwind läuft weiter.",
                "entities": ["PTI_PROJEKT"],
            },
        )
        assert r.status_code == 200, r.text
        funde = r.json()["entities"]
        assert [f["entity_type"] for f in funde] == ["PTI_PROJEKT"], funde
        assert funde[0]["text"] == "Nordwind"
    finally:
        weg = client.delete(f"/api/v1/terms/{angelegt.json()['id']}")
        assert weg.status_code == 200, weg.text
