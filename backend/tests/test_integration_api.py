"""End-to-End-Test der gesamten Kette ueber die echte HTTP-Schnittstelle.

Faehrt die Anwendung so hoch, wie sie in Produktion laeuft — nur mit
abgeschaltetem SSO-Gate (``HUB_AUTH=off``) und einer In-Memory-Datenbank.
Geprueft wird das, was der Nutzer tatsaechlich tut: Text hineingeben,
Pseudonyme herausbekommen, und den Text spaeter wieder herstellen.

Dies ist der einzige Test, der Erkennung, Vault und Formate zusammen sieht.
"""

from __future__ import annotations

import base64
import io
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Testtext mit je einer Entitaet pro Erkennungsebene. Frei erfunden —
#: es gehoeren keine echten Personendaten in dieses Repository.
#:
#: Echte Umlaute sind hier Pflicht, nicht Kosmetik: das deutsche NER-Modell
#: erkennt transliterierte Formen schlechter und erfindet dort Personennamen
#: (siehe test_transliterierte_umlaute_erzeugen_fehlalarme).
TEXT = (
    "Herr Andreas Meier, wohnhaft an der Bahnhofstrasse 12 in 8001 Zürich, "
    "erreichbar unter andreas.meier@example.ch. "
    "IBAN CH93 0076 2011 6238 5295 7. "
    "Die Kantonspolizei Zürich führt das Verfahren im Jahr 2026."
)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Faehrt die Anwendung hoch, ohne die globale Umgebung zu verschmutzen.

    Die Umgebungsvariablen werden per ``MonkeyPatch`` gesetzt und danach
    zurueckgenommen. Frueher standen sie auf Modulebene — das lief schon
    beim Einsammeln der Tests und liess vier Krypto-Tests scheitern, die
    ihrerseits mit ``VAULT_MASTER_KEY`` arbeiten. Der Cache von
    ``get_settings`` wird beidseitig geleert, sonst wirkt die Aenderung
    entweder gar nicht oder viel zu lange.
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


def test_health_ist_ohne_anmeldung_erreichbar(client: TestClient) -> None:
    """Der Hausstandard aller PTI-Apps, vom SSO-Gate ausgenommen."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_me_liefert_den_entwicklungsbenutzer(client: TestClient) -> None:
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["username"] == "dev"
    assert r.json()["isAdmin"] is True


def test_detect_findet_die_erwarteten_entitaeten(client: TestClient) -> None:
    """Erkennung ohne Ersetzung — die Grundlage der Befund-Ansicht."""
    r = client.post("/api/v1/documents:detect", json={"text": TEXT})
    assert r.status_code == 200, r.text
    typen = {e["entity_type"] for e in r.json()["entities"]}

    # Ebene 2 (NER) und Ebene 1 (Muster mit Pruefziffer) muessen beide greifen.
    assert "PERSON" in typen
    assert "EMAIL_ADDRESS" in typen
    assert typen & {"IBAN_CODE", "CH_IBAN"}, f"keine IBAN erkannt, gefunden: {typen}"

    # Jeder Fund traegt ein deutsches Label und eine Ampelstufe.
    for e in r.json()["entities"]:
        assert e["label"]
        assert e["confidence"] in {"gruen", "gelb", "rot"}

    # Kein Fund darf sich mit einem anderen ueberlappen — engine.merge muss
    # das aufgeloest haben, sonst zerstoert die Ersetzung spaeter den Text.
    spans = sorted((e["start"], e["end"]) for e in r.json()["entities"])
    for (_, ende), (start, _) in zip(spans, spans[1:]):
        assert ende <= start, f"Ueberlappung bei {spans}"


def test_jahreszahl_wird_nicht_als_postleitzahl_erkannt(client: TestClient) -> None:
    """Die Precision-Falle: '2026' im Fliesstext ist keine PLZ."""
    r = client.post("/api/v1/documents:detect", json={"text": "Im Jahr 2026 geschah es."})
    assert r.status_code == 200
    assert not [e for e in r.json()["entities"] if e["entity_type"] == "CH_POSTAL_CODE"]


def test_pseudonymisieren_und_rueckfuehren_ist_verlustfrei(client: TestClient) -> None:
    """Der zentrale Nachweis: der Rueckweg stellt das Original exakt wieder her."""
    r = client.post(
        "/api/v1/documents:anonymize",
        json={"text": TEXT, "mode": "reversible", "vault_label": "Testlauf"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    vault_id = body["vault_id"]
    assert vault_id, "im umkehrbaren Modus muss ein Vault entstehen"

    # Der Name darf im Ergebnis nicht mehr vorkommen.
    assert "Andreas Meier" not in body["text"]
    assert "[[" in body["text"], "es wurde nichts ersetzt"

    zurueck = client.post(
        "/api/v1/documents:reverse", json={"text": body["text"], "vault_id": vault_id}
    )
    assert zurueck.status_code == 200, zurueck.text
    assert zurueck.json()["text"] == TEXT, "Rueckfuehrung ist nicht verlustfrei"


def test_pseudonyme_sind_ueber_laeufe_hinweg_stabil(client: TestClient) -> None:
    """Derselbe Name bekommt im selben Vault immer dasselbe Pseudonym."""
    erst = client.post(
        "/api/v1/documents:anonymize",
        json={"text": "Andreas Meier war da.", "vault_label": "Stabil"},
    ).json()
    vault_id = erst["vault_id"]

    zweit = client.post(
        "/api/v1/documents:anonymize",
        json={"text": "Später kam Andreas Meier erneut.", "vault_id": vault_id},
    ).json()

    import re

    p1 = set(re.findall(r"\[\[[A-Z_]+_\d+\]\]", erst["text"]))
    p2 = set(re.findall(r"\[\[[A-Z_]+_\d+\]\]", zweit["text"]))
    assert p1 and p1 == p2, f"Pseudonym nicht stabil: {p1} vs {p2}"


def test_transliterierte_umlaute_erzeugen_fehlalarme(client: TestClient) -> None:
    """Dokumentiert eine gemessene Schwaeche des deutschen NER-Modells.

    ``de_core_news_md`` haelt das transliterierte ``Spaeter`` fuer einen
    Personennamen, das korrekt geschriebene ``Später`` dagegen nicht — das Wort
    in Ersatzschreibweise stand nie in den Trainingsdaten und sieht damit aus
    wie ein unbekanntes Eigennamen-Token.

    Folgen fuer den Betrieb:

    * Eingaben sollten echte Umlaute enthalten. Wer ae/oe/ue schreibt, bekommt
      mehr Fehlalarme.
    * Testdaten in diesem Repository muessen deshalb ebenfalls echte Umlaute
      verwenden, sonst misst das Golden-Set die falsche Groesse.

    Dieser Test haelt den Befund fest. Schlaegt er eines Tages fehl, hat sich
    das Modell verbessert — dann darf er entfallen.
    """
    mit_umlaut = client.post(
        "/api/v1/documents:detect", json={"text": "Später kam Andreas Meier erneut."}
    ).json()["entities"]
    transliteriert = client.post(
        "/api/v1/documents:detect", json={"text": "Spaeter kam Andreas Meier erneut."}
    ).json()["entities"]

    personen_mit = [e["text"] for e in mit_umlaut if e["entity_type"] == "PERSON"]
    personen_ohne = [e["text"] for e in transliteriert if e["entity_type"] == "PERSON"]

    assert personen_mit == ["Andreas Meier"]
    assert "Spaeter" in personen_ohne, (
        "Der bekannte Fehlalarm ist verschwunden — Modell aktualisiert? "
        f"Gefunden: {personen_ohne}"
    )


def test_anonymer_modus_legt_keinen_vault_an(client: TestClient) -> None:
    """Ohne Vault ist der Vorgang endgueltig — das muss sichtbar sein."""
    r = client.post(
        "/api/v1/documents:anonymize", json={"text": TEXT, "mode": "anonymous"}
    )
    assert r.status_code == 200
    assert r.json()["vault_id"] is None


def test_unbekannter_modus_wird_abgelehnt(client: TestClient) -> None:
    r = client.post("/api/v1/documents:anonymize", json={"text": "x", "mode": "quatsch"})
    assert r.status_code == 400


def test_begriffsliste_wirkt_auf_die_erkennung(client: TestClient) -> None:
    """Ebene 0 schlaegt jede Heuristik: was in der Liste steht, wird ersetzt."""
    angelegt = client.post(
        "/api/v1/terms",
        json={"term": "Projekt Nordlicht", "entity_type": "ORGANIZATION", "match_mode": "normalized"},
    )
    assert angelegt.status_code == 201, angelegt.text
    term_id = angelegt.json()["id"]

    try:
        r = client.post(
            "/api/v1/documents:detect", json={"text": "Das Projekt Nordlicht startet."}
        )
        treffer = [e for e in r.json()["entities"] if "Nordlicht" in e["text"]]
        assert treffer, "Begriff aus der Liste wurde nicht gefunden"
        assert treffer[0]["confidence"] == "gruen"
    finally:
        assert client.delete(f"/api/v1/terms/{term_id}").status_code == 200


def test_doppelter_begriff_wird_abgelehnt(client: TestClient) -> None:
    a = client.post("/api/v1/terms", json={"term": "Deckname Alpha"})
    assert a.status_code == 201
    try:
        b = client.post("/api/v1/terms", json={"term": "Deckname Alpha"})
        assert b.status_code == 409
    finally:
        client.delete(f"/api/v1/terms/{a.json()['id']}")


def test_vault_liste_zeigt_keine_originale(client: TestClient) -> None:
    """Der Klartext darf ueber diesen Weg nicht herausfallen."""
    angelegt = client.post(
        "/api/v1/documents:anonymize",
        json={"text": "Andreas Meier schrieb.", "vault_label": "Geheim"},
    ).json()

    detail = client.get(f"/api/v1/mappings/{angelegt['vault_id']}")
    assert detail.status_code == 200
    roh = detail.text
    assert "Andreas" not in roh, "Der Klartext darf in der Vault-Ansicht nicht auftauchen"
    assert detail.json()["entries"], "es sollte Eintraege geben"


def test_unbekannter_vault_liefert_404(client: TestClient) -> None:
    r = client.get("/api/v1/mappings/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_kaputte_kennung_liefert_400_nicht_500(client: TestClient) -> None:
    r = client.get("/api/v1/mappings/keine-uuid")
    assert r.status_code == 400


def test_docx_durchlauf_erhaelt_das_format(client: TestClient) -> None:
    """Datei rein, Datei raus — und das Ergebnis ist wieder ein gueltiges DOCX."""
    docx = pytest.importorskip("docx")

    puffer = io.BytesIO()
    dokument = docx.Document()
    absatz = dokument.add_paragraph()
    # Bewusst ueber drei Runs verteilt: genau der Fall, an dem eine naive
    # Ersetzung scheitert.
    absatz.add_run("Andreas ")
    absatz.add_run("Mei")
    absatz.add_run("er hat unterschrieben.")
    dokument.save(puffer)

    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={
            "file": (
                "test.docx",
                puffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"mode": "reversible", "vault_label": "Dateitest"},
    )
    assert r.status_code == 200, r.text
    ergebnis = base64.b64decode(r.json()["content_base64"])

    wieder = docx.Document(io.BytesIO(ergebnis))
    text = "\n".join(p.text for p in wieder.paragraphs)
    assert "Andreas Meier" not in text, f"Name ueber Run-Grenzen nicht ersetzt: {text!r}"
    assert "hat unterschrieben." in text, f"Resttext beschaedigt: {text!r}"


def test_unbekanntes_format_liefert_415(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("bild.png", b"\x89PNG\r\n", "image/png")},
        data={"mode": "anonymous"},
    )
    assert r.status_code == 415
