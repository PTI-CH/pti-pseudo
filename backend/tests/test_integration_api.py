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
import zipfile
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


def test_transliterierte_umlaute_fangen_wir_ab(client: TestClient) -> None:
    """Die Wortart-Regel faengt eine gemessene Schwaeche des NER-Modells ab.

    ``de_core_news_md`` haelt das transliterierte ``Spaeter`` fuer einen
    Personennamen, das korrekt geschriebene ``Später`` dagegen nicht — das Wort
    in Ersatzschreibweise stand nie in den Trainingsdaten und sieht damit aus
    wie ein unbekanntes Eigennamen-Token.

    **Das Modell tut das unveraendert.** Nachgemessen am 2026-08-24: Der rohe
    Presidio-Lauf meldet ``Spaeter`` weiterhin als PERSON. Neu ist, dass
    ``backend.engine.wortarten`` den Fund verwirft, weil dasselbe Modell das
    Token als ``NOUN`` und nicht als ``PROPN`` fuehrt — die beiden Schichten
    des Modells widersprechen sich, und die Wortart hat recht.

    Der Betriebshinweis bleibt trotzdem gueltig: Eingaben sollten echte
    Umlaute enthalten, und Testdaten in diesem Repository erst recht. Die
    Regel greift nur bei **einzelnen** Tokens; ein transliterierter Name aus
    Vor- und Nachname geht weiterhin als Fehlalarm durch.
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
    assert personen_ohne == ["Andreas Meier"], (
        "Der Fehlalarm auf 'Spaeter' ist zurueck — greift die Wortart-Regel "
        f"nicht mehr? Gefunden: {personen_ohne}"
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


def test_zeitstempel_tragen_eine_zonenangabe(client: TestClient) -> None:
    """Jeder ausgelieferte Zeitstempel muss zonenbewusst sein.

    Unter SQLite kommen die Werte naiv aus der Datenbank zurueck. Ein blosses
    ``isoformat()`` schriebe sie ohne ``+00:00``, und ein Browser liest einen
    ISO-String ohne Zone als **Lokalzeit** — im Sommer zwei Stunden daneben.
    Geprueft werden Vault-Liste, Vault-Detail und Begriffsliste zusammen, weil
    genau diese drei Antwortformen die Korrektur zuletzt vermissen liessen.
    """
    angelegt = client.post(
        "/api/v1/documents:anonymize",
        json={"text": "Andreas Meier schrieb.", "vault_label": "Zeitzone"},
    ).json()
    client.post("/api/v1/terms", json={"term": "Musterbehoerde", "entity_type": "ORGANIZATION"})

    liste = client.get("/api/v1/mappings").json()["mappings"]
    assert liste, "es sollte einen Vault geben"
    detail = client.get(f"/api/v1/mappings/{angelegt['vault_id']}").json()
    begriffe = client.get("/api/v1/terms").json()["terms"]
    assert begriffe, "es sollte einen Begriff geben"

    zeitstempel = [v["created_at"] for v in liste]
    zeitstempel.append(detail["created_at"])
    zeitstempel += [e["created_at"] for e in detail["entries"]]
    zeitstempel += [b["created_at"] for b in begriffe]

    for wert in zeitstempel:
        assert wert.endswith("+00:00") or wert.endswith("Z"), (
            f"Zeitstempel ohne Zonenangabe: {wert!r} — "
            "gehoert durch backend.vault.models.iso_utc"
        )


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


def test_xlsx_durchlauf_erhaelt_das_format(client: TestClient) -> None:
    """Mappe rein, Mappe raus — und die gemeinsame Zeichenkette bleibt heil.

    Der Weg durch die HTTP-Schicht, nicht nur durch den Adapter: Ein Upload
    mit dem MIME-Typ, den Excel schickt, muss beim XlsxAdapter landen und ein
    oeffenbares Ergebnis zurueckgeben.
    """
    from backend.tests.ooxml_fabrik import baue_xlsx

    mappe = baue_xlsx(
        [("Beteiligte", [["Nachname", "Vorname"], ["Zwicky", "Marlene"]])]
    )
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={
            "file": (
                "beteiligte.xlsx",
                mappe,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"mode": "reversible", "vault_label": "Mappentest"},
    )
    assert r.status_code == 200, r.text
    ergebnis = base64.b64decode(r.json()["content_base64"])

    with zipfile.ZipFile(io.BytesIO(ergebnis)) as zf:
        assert zf.testzip() is None, "das Ergebnis ist kein oeffenbares XLSX"
        blatt = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Marlene" not in blatt, f"Name nicht ersetzt: {blatt!r}"
    # Die Ueberschriften stehen in der gemeinsamen Tabelle und duerfen dort
    # unberuehrt bleiben — sonst haette die Ersetzung ueber die Zelle
    # hinausgewirkt.
    with zipfile.ZipFile(io.BytesIO(mappe)) as vorher:
        with zipfile.ZipFile(io.BytesIO(ergebnis)) as nachher:
            assert vorher.read("xl/sharedStrings.xml") == nachher.read(
                "xl/sharedStrings.xml"
            )


def test_pptx_durchlauf_erfasst_die_notizen(client: TestClient) -> None:
    """Der Name in den Notizen muss ebenso verschwinden wie der auf der Folie.

    Eine Praesentation, bei der nur die Folien pseudonymisiert sind, *sieht*
    sauber aus — die Notizen des Vortragenden liest niemand nach.
    """
    from backend.tests.ooxml_fabrik import baue_pptx

    praesentation = baue_pptx(
        [([["Es schrieb ", "Andrea ", "Bei", "spiel."]], ["Auskunft: Beat Kunz."])]
    )
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={
            "file": (
                "lage.pptx",
                praesentation,
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation",
            )
        },
        data={"mode": "reversible", "vault_label": "Folientest"},
    )
    assert r.status_code == 200, r.text
    ergebnis = base64.b64decode(r.json()["content_base64"])

    with zipfile.ZipFile(io.BytesIO(ergebnis)) as zf:
        assert zf.testzip() is None, "das Ergebnis ist kein oeffenbares PPTX"
        folie = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        notizen = zf.read("ppt/notesSlides/notesSlide1.xml").decode("utf-8")
    assert "Andrea" not in folie, f"Name ueber Run-Grenzen nicht ersetzt: {folie!r}"
    assert "Beat Kunz" not in notizen, f"Name in den Notizen blieb stehen: {notizen!r}"


def test_unbekanntes_format_liefert_415(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("bild.png", b"\x89PNG\r\n", "image/png")},
        data={"mode": "anonymous"},
    )
    assert r.status_code == 415


def test_riesiger_text_liefert_413_statt_absturz(client: TestClient) -> None:
    """Ungeprueft haut ein sehr grosser Text die Erkennungs-Engine um.

    ``spaCy`` bricht intern mit einem rohen ``ValueError`` ab, sobald ein Text
    dessen ``nlp.max_length`` (Default 1'000'000 Zeichen) uebersteigt — anders
    als beim Datei-Upload (``_check_size``) gab es fuer den JSON-Textpfad keine
    Obergrenze, die das vorher abfaengt. Ohne Fix landet hier ein 500 (oder,
    durch das SSO-Gate verschluckt, sogar ein irrefuehrendes 401) statt eines
    sauberen 413.
    """
    riesig = "A" * 2_000_000
    r = client.post("/api/v1/documents:detect", json={"text": riesig})
    assert r.status_code == 413, r.text

    r2 = client.post(
        "/api/v1/documents:anonymize", json={"text": riesig, "mode": "anonymous"}
    )
    assert r2.status_code == 413, r2.text


def test_riesiger_text_liefert_auch_bei_reverse_413(client: TestClient) -> None:
    """Die Rueckfuehrung hatte als einziger Textpfad keine Obergrenze.

    Gemessen wird an der Reihenfolge: die Groessenpruefung muss **vor** dem
    Nachschlagen des Vaults greifen. Die Kennung hier existiert nicht — kaeme
    die Pruefung zu spaet, saehe man ein 404 statt des 413.
    """
    riesig = "A" * 2_000_000
    r = client.post(
        "/api/v1/documents:reverse",
        json={"text": riesig, "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"},
    )
    assert r.status_code == 413, r.text
    assert "300000" in r.json()["detail"] or "Zeichen" in r.json()["detail"]
