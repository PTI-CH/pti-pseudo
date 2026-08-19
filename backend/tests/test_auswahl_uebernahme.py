"""Prueft ``selected_entities`` — die geprüfte Befund-Auswahl — ueber die REST-API.

Warum es diese Datei gibt
-------------------------
Die Oberflaeche zeigt jeden Fund mit einem Kaestchen und darunter den Knopf
„Anwenden". Ob das Abwaehlen eines Fehlalarms tatsaechlich wirkt, haengt an
einem Vertrag, den nichts geprueft hat: Der Server erkennt einen Fund am Tripel
``(segment_id, start, end)``. Fehlt ``segment_id`` in der Auswahl, passt sie auf
keinen einzigen Fund — der Aufruf meldet dann **200 und ersetzt nichts**.
Genau das war der Zustand: die Oberflaeche schickte den Abschnitt nicht mit.

Ein stiller Totalausfall dieser Art faellt niemandem auf, solange ihn kein Test
festhaelt. Deshalb steht hier je ein Test fuer den Text- und den Dateipfad.

Die Fixture ist wie in ``test_entities_filter.py`` bewusst kopiert statt
importiert — jede Testdatei soll unabhaengig lesbar bleiben.
"""

from __future__ import annotations

import base64
import json
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Frei erfunden — es gehoeren keine echten Personendaten in dieses Repository.
TEXT = "Petra Müller meldet sich. Beat Vogel ist zustaendig."


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "c" * 64)

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


def _funde(client: TestClient) -> list[dict]:
    r = client.post(
        "/api/v1/documents:detect", json={"text": TEXT, "entities": ["PERSON"]}
    )
    assert r.status_code == 200, r.text
    funde = r.json()["entities"]
    assert funde, "Ohne Funde prueft dieser Test nichts."
    return funde


# ── Textpfad ───────────────────────────────────────────────────────────────


def test_auswahl_mit_segment_id_wirkt(client: TestClient) -> None:
    """Der Regressionsfall: mit ``segment_id`` wird auch wirklich ersetzt."""
    funde = _funde(client)

    r = client.post(
        "/api/v1/documents:anonymize",
        json={
            "text": TEXT,
            "mode": "anonymous",
            "entities": ["PERSON"],
            "selected_entities": funde,
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == len(funde)
    assert "Petra Müller" not in r.json()["text"]


def test_auswahl_ohne_segment_id_ersetzt_nichts(client: TestClient) -> None:
    """Haelt fest, was der Vertrag verlangt — und was passiert, wenn er bricht.

    Kein Wunschverhalten, sondern eine Warntafel: Wer ``segment_id`` weglaesst,
    bekommt 200 und ein unveraendertes Dokument. Aendert sich das je zu einer
    toleranteren Auslegung, soll dieser Test bewusst rot werden und nicht
    unbemerkt mitwandern.
    """
    ohne = [
        {k: v for k, v in f.items() if k != "segment_id"} for f in _funde(client)
    ]

    r = client.post(
        "/api/v1/documents:anonymize",
        json={
            "text": TEXT,
            "mode": "anonymous",
            "entities": ["PERSON"],
            "selected_entities": ohne,
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 0


def test_leere_auswahl_ersetzt_nichts(client: TestClient) -> None:
    """Alles abgewaehlt heisst alles abgewaehlt — nicht „Vorgabe anwenden"."""
    r = client.post(
        "/api/v1/documents:anonymize",
        json={
            "text": TEXT,
            "mode": "anonymous",
            "entities": ["PERSON"],
            "selected_entities": [],
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 0
    assert r.json()["text"] == TEXT


# ── Dateipfad ──────────────────────────────────────────────────────────────


def _datei_funde(client: TestClient) -> list[dict]:
    """Funde aus einem Dateilauf — mit den Segment-IDs *dieses* Adapters.

    Bewusst nicht die Funde aus ``:detect`` wiederverwendet: dort heisst das
    Segment ``text``, im Dateipfad tragen die Segmente die Adressen des
    Adapters. Eine Auswahl aus dem einen Lauf passt nicht auf den anderen —
    genau die Falle, die dieser Datei zugrunde liegt.
    """
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "anonymous", "entities": "PERSON"},
    )
    assert r.status_code == 200, r.text
    funde = r.json()["entities"]
    assert funde, "Ohne Funde prueft dieser Test nichts."
    return funde


def test_datei_auswahl_wirkt(client: TestClient) -> None:
    """Ohne dieses Feld waeren die Kaestchen bei Dateien blosse Zierde."""
    funde = _datei_funde(client)
    behalten = [f for f in funde if f["text"] != "Petra Müller"]
    assert len(behalten) < len(funde), "Der Testtext liefert nicht den erwarteten Fund."

    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={
            "mode": "anonymous",
            "entities": "PERSON",
            "selected_entities": json.dumps(behalten),
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == len(behalten)
    inhalt = base64.b64decode(r.json()["content_base64"]).decode("utf-8")
    # Der abgewaehlte Fund steht noch da, die uebrigen sind ersetzt.
    assert "Petra Müller" in inhalt
    assert "Beat Vogel" not in inhalt


def test_datei_ohne_auswahl_ersetzt_weiterhin_alles(client: TestClient) -> None:
    """Ein fehlendes Feld ist die Vorgabe, keine leere Auswahl."""
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "anonymous", "entities": "PERSON"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] > 0
    inhalt = base64.b64decode(r.json()["content_base64"]).decode("utf-8")
    assert "Petra Müller" not in inhalt


def test_datei_leere_auswahl_ersetzt_nichts(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "anonymous", "entities": "PERSON", "selected_entities": "[]"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 0
    inhalt = base64.b64decode(r.json()["content_base64"]).decode("utf-8")
    assert inhalt == TEXT


def test_datei_auswahl_kein_json_gibt_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "anonymous", "entities": "PERSON", "selected_entities": "{"},
    )

    assert r.status_code == 400
    assert "JSON" in r.json()["detail"]


def test_datei_auswahl_falsche_form_gibt_400(client: TestClient) -> None:
    """Eine Liste von Zahlen ist kein Befund — 400 statt stiller Wirkungslosigkeit."""
    r = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "anonymous", "entities": "PERSON", "selected_entities": "[1, 2]"},
    )

    assert r.status_code == 400
    assert "segment_id" in r.json()["detail"]
