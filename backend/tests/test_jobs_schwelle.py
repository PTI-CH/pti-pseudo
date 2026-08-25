"""Prueft die Groessenschwelle der Dateirouten.

Unterhalb der Schwelle laeuft eine Datei synchron durch — das ist die
bestehende Zusage und darf sich nicht aendern. Oberhalb wird daraus ein
Auftrag (202 statt 200) -- aber nur, wenn diese Installation ueberhaupt
einen Hintergrundarbeiter faehrt (``warteschlange.
hintergrundverarbeitung_moeglich``). Faehrt sie keinen (Testbetrieb, und die
Ein-Knoten-SQLite-Rueckfallvariante aus ``JELASTIC_SETUP.md`` 1b), lehnt die
Route mit 503 ab, statt eine Auftragszeile anzulegen, die nie jemand
abarbeitet. Die Textrouten bleiben davon in jedem Fall unberuehrt, weil sie
ueber ``max_text_chars`` begrenzt sind und in Sekundenbruchteilen laufen.

Die Fixture ist wie in ``test_auswahl_uebernahme.py`` bewusst kopiert statt
importiert — es gibt in diesem Baum keine ``conftest.py``, jede Testdatei
bringt ihre eigene Vorrichtung mit.
"""

from __future__ import annotations

import json
import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


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


def _lade_auftrag(job_id: str):
    """Holt einen Auftrag direkt aus der Datenbank, an ``client`` vorbei.

    Die API gibt ``parameter_json`` bewusst nicht ueber ``GET /jobs/{id}``
    zurueck (die Statusroute traegt keine Dokumentinhalte, siehe
    ``routers/jobs.py``) — fuer die Pruefung selbst muss dieser Test also
    an der Datenbank vorbeischauen.
    """
    from backend.db import get_sessionmaker
    from backend.vault.models import Job

    with get_sessionmaker()() as s:
        return s.get(Job, uuid.UUID(job_id))


def test_kleine_datei_bleibt_synchron(client):
    """Unterhalb der Schwelle aendert sich nichts — das ist die Zusage."""
    from backend.db import get_sessionmaker
    from backend.vault.models import Job

    antwort = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")},
        data={"mode": "anonymous"},
    )
    assert antwort.status_code == 200
    assert "job_id" not in antwort.json()
    assert "content_base64" in antwort.json()
    with get_sessionmaker()() as s:
        # Kein Umweg ueber die Warteschlange: unterhalb der Schwelle darf
        # dabei keine Zeile in der Auftragstabelle entstehen.
        assert s.query(Job).count() == 0


def test_grosse_datei_wird_ein_auftrag(client, monkeypatch):
    """Oberhalb der Schwelle wird ein Auftrag mit vollstaendigen Parametern.

    Der zweite Teil dieses Tests ist der eigentliche Wächter: Fehlt
    ``selected_entities`` in ``parameter_json``, ersetzt der Auftrag beim
    Ausfuehren stillschweigend ALLE gefundenen Stellen statt nur der vom
    Menschen bestaetigten — am 2026-08-25 mit einem Wegwerf-Skript
    nachgewiesen (siehe Aufgabenbericht): eine bewusst abgewaehlte
    Fundstelle wurde trotzdem ersetzt. Der Fehler faellt dabei niemandem
    auf, weil das Ergebnisdokument trotzdem pseudonymisiert aussieht — nur
    eben mehr, als der Mensch wollte. Ohne diesen Test koennte ein
    kuenftiger Umbau diesen Schluessel verlieren, ohne dass irgendein Test
    rot wird.
    """
    from backend.config import get_settings
    from backend.jobs import warteschlange

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    # Dieser Test prueft die Parameter eines angelegten Auftrags, nicht die
    # Dialekt-Schranke -- die weist auf dem echten SQLite-Testbetrieb jede
    # grosse Datei mit 503 ab (siehe
    # ``test_grosse_datei_ohne_hintergrundverarbeitung_wird_klar_abgelehnt``
    # unten). Hier wird "verfuegbar" vorgetaeuscht, damit ausschliesslich
    # ``parameter_json`` geprueft wird.
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")
            },
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 202
        assert antwort.json()["status"] == "wartend"
        job_id = antwort.json()["job_id"]
        assert job_id

        job = _lade_auftrag(job_id)
        assert job is not None
        parameter = job.parameter_json
        # Auf Anwesenheit pruefen, nicht auf bestimmte Werte — eine
        # harmlose Erweiterung der Parameterliste soll diesen Test nicht
        # zum Reissen bringen. Wichtig ist einzig, dass keiner der
        # Schluessel, die der Lauf braucht, verlorengeht.
        for schluessel in (
            "modus",
            "vault_id",
            "vault_label",
            "entities",
            "selected_entities",
            "ki_pruefung",
        ):
            assert schluessel in parameter, f"{schluessel} fehlt in parameter_json"
    finally:
        get_settings.cache_clear()


def test_auftrag_speichert_keinen_klartext_der_fundauswahl(client, monkeypatch):
    """Waechter fuer Fund 1: kein Klartext in `parameter_json`.

    `parameter_json` ist eine normale, unverschluesselte JSONB-Spalte. Der
    Aufrufer (Oberflaeche oder ein Aufrufer ueber `/api/ext/v1`) darf in
    `selected_entities` weiterhin `text` mitschicken -- die Route soll das
    tolerieren, ohne es zu uebernehmen. Vor der Behebung landete der
    Klartext eines bestaetigten Fundes ungefiltert in der Datenbank; dieser
    Test schickt ein Dokument mit einem wiedererkennbaren Klarnamen durch
    den Auftragsweg und prueft, dass dieser Name nirgends im gespeicherten
    `parameter_json` auftaucht.
    """
    from backend.config import get_settings
    from backend.jobs import warteschlange

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    try:
        klartext = "Zbinden"
        auswahl = json.dumps(
            [
                {
                    "segment_id": "text",
                    "start": 5,
                    "end": 19,
                    "entity_type": "PERSON",
                    "text": f"Sandra {klartext}",
                    "score": 0.99,
                }
            ]
        )
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")
            },
            data={"mode": "anonymous", "selected_entities": auswahl},
        )
        assert antwort.status_code == 202
        job = _lade_auftrag(antwort.json()["job_id"])
        assert job is not None

        roh = json.dumps(job.parameter_json)
        assert klartext not in roh, "der Nachname darf nicht in parameter_json stehen"
        assert "Sandra" not in roh, "der Vorname darf nicht in parameter_json stehen"
        # Die Auswahl selbst muss trotzdem funktionsfaehig bleiben.
        eintrag = job.parameter_json["selected_entities"][0]
        assert eintrag["segment_id"] == "text"
        assert eintrag["start"] == 5
        assert eintrag["end"] == 19
    finally:
        get_settings.cache_clear()


def test_grosser_reversibler_auftrag_traegt_den_dateinamen_als_vault_label(
    client, monkeypatch
):
    """Waechter fuer Fund 3: ein Auftrags-Tresor ohne eigene Bezeichnung
    erbt den Dateinamen, genau wie der synchrone Pfad.

    Die Oberflaeche schickt ``vault_label`` nie mit. Ohne die Rueckfallregel
    heissen deshalb ALLE ueber einen Auftrag angelegten Tresore „Ohne
    Bezeichnung" -- wer mehrere grosse Akten umkehrbar verarbeitet hat, kann
    sie in der Tresorliste nicht mehr unterscheiden. Greift er zur falschen,
    meldet die Rueckfuehrung keinen Fehler, sondern findet nur kein
    Pseudonym und gibt das Dokument unveraendert zurueck.
    """
    from backend.config import get_settings
    from backend.db import get_sessionmaker
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus
    from backend.vault.models import Vault

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    try:
        original = b"Frau Sandra Zbinden war da."
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={"file": ("wichtige_akte.txt", original, "text/plain")},
            data={"mode": "reversible"},
        )
        assert antwort.status_code == 202
        job = _lade_auftrag(antwort.json()["job_id"])
        assert job is not None
        assert job.parameter_json.get("vault_label") == "wichtige_akte.txt", (
            "der Dateiname muss schon in parameter_json als Rueckfall stehen"
        )

        ergebnis = fuehre_aus(job, original, lambda a, b: None)
        with get_sessionmaker()() as s:
            vault = s.get(Vault, uuid.UUID(ergebnis.zusatz["vault_id"]))
            assert vault is not None
            assert vault.label == "wichtige_akte.txt"
    finally:
        get_settings.cache_clear()


def test_grosse_reverse_datei_traegt_vault_id(client, monkeypatch):
    """Auch die Rueckfuehrung legt ihren einzigen Parameter vollstaendig ab.

    Anders als bei ``selected_entities`` waere ein Verlust hier nicht
    still: ``ausfuehrung.fuehre_aus`` verlangt ``vault_id`` und laesst den
    Auftrag ohne sie mit einer klaren Fehlermeldung scheitern (Status
    ``fehler`` statt eines falschen Ergebnisses). Trotzdem: ohne diesen
    Test war der Auftragspfad von ``reverse-file`` bislang ganz
    ungeprueft — dieser Test schliesst die Luecke fuer wenig Aufwand.
    """
    from backend.config import get_settings

    # Einen Vault anlegen, dessen Kennung wir gleich mitfuehren.
    anlage = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")},
        data={"mode": "reversible"},
    )
    assert anlage.status_code == 200, anlage.text
    vault_id = anlage.json()["vault_id"]
    assert vault_id

    from backend.jobs import warteschlange

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    # Dieselbe Vortaeuschung wie oben, aus demselben Grund: dieser Test
    # prueft ``parameter_json``, nicht die Dialekt-Schranke.
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    try:
        antwort = client.post(
            "/api/v1/documents:reverse-file",
            files={"file": ("notiz.txt", b"[[PERSON]] war da.", "text/plain")},
            data={"vault_id": vault_id},
        )
        assert antwort.status_code == 202
        job = _lade_auftrag(antwort.json()["job_id"])
        assert job is not None
        assert "vault_id" in job.parameter_json
    finally:
        get_settings.cache_clear()


def test_die_textroute_bleibt_immer_synchron(client, monkeypatch):
    """Die haeufigste Nutzung darf keinen Umweg bekommen."""
    from backend.config import get_settings

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "1")
    get_settings.cache_clear()
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize",
            json={"text": "Frau Sandra Zbinden war da.", "mode": "anonymous"},
        )
        assert antwort.status_code == 200
    finally:
        get_settings.cache_clear()


def test_grosse_datei_ohne_hintergrundverarbeitung_wird_klar_abgelehnt(
    client, monkeypatch
):
    """Kein Auftrag ohne Arbeiter -- die eigentliche Lehre aus Aufgabe 10.

    Diese ``client``-Fixture laeuft auf echtem SQLite, ungepatcht: genau der
    Zustand des Testbetriebs und der Ein-Knoten-Rueckfallvariante
    (``JELASTIC_SETUP.md`` 1b), in dem der Auftragsarbeiter nie startet
    (``backend/main.py``). Vor diesem Fix bekam eine grosse Datei dort
    trotzdem eine ``job_id`` und blieb für immer auf ``wartend`` stehen, ohne
    dass irgendwo stand, warum. Jetzt entsteht gar keine Auftragszeile mehr,
    und die Antwort sagt dem Menschen, was zu tun ist.
    """
    from backend.config import get_settings
    from backend.db import get_sessionmaker
    from backend.vault.models import Job

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    # ``client`` ist modulweit geteilt -- andere Tests in dieser Datei legen
    # (gepatcht) selbst Auftraege an. Gezaehlt wird deshalb die Differenz,
    # nicht ein absoluter Wert.
    with get_sessionmaker()() as s:
        vorher = s.query(Job).count()
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")
            },
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 503
        assert "job_id" not in antwort.json()
        meldung = antwort.json()["detail"]
        assert "Administration" in meldung or "teilen" in meldung

        with get_sessionmaker()() as s:
            # Der wichtigere Teil: keine NEUE Zeile, die niemand je abarbeitet.
            assert s.query(Job).count() == vorher
    finally:
        get_settings.cache_clear()


def test_grosse_reverse_datei_ohne_hintergrundverarbeitung_wird_klar_abgelehnt(
    client, monkeypatch
):
    """Dasselbe fuer die Rueckfuehrung -- derselbe Codepfad, eigener Test."""
    from backend.config import get_settings
    from backend.db import get_sessionmaker
    from backend.vault.models import Job

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    with get_sessionmaker()() as s:
        vorher = s.query(Job).count()
    try:
        antwort = client.post(
            "/api/v1/documents:reverse-file",
            files={"file": ("notiz.txt", b"[[PERSON]] war da.", "text/plain")},
            data={"vault_id": str(uuid.uuid4())},
        )
        assert antwort.status_code == 503
        assert "job_id" not in antwort.json()

        with get_sessionmaker()() as s:
            assert s.query(Job).count() == vorher
    finally:
        get_settings.cache_clear()
