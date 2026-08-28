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


def test_dieselbe_absage_unabhaengig_von_der_dateigroesse(client, monkeypatch):
    """Eine .doc bekommt 415 — klein wie gross, und mit demselben Hinweis.

    Vor dieser Nachbesserung antwortete dieselbe Datei je nach GROESSE
    verschieden: unterhalb der Schwelle sofort 415 mit „bitte als .docx
    speichern", oberhalb eine 202 mit ``job_id``, und der Formatfehler kam
    erst Minuten spaeter als gescheiterter Auftrag zurueck — in einem Feld,
    das der Mensch fuer eine Verarbeitungspanne haelt, nicht fuer eine
    Formatabsage. Bis dahin laege die Datei verschluesselt in der Datenbank,
    fuer einen Lauf, der von vornherein nicht gelingen konnte.

    Das alte Word-Format (``.doc``) ist die Wahl, weil PDF seit der
    Textebenen-Unterstuetzung ein unterstuetztes Format ist — ``.doc`` steht
    weiterhin in ``adapters._PLANNED`` und eignet sich damit unveraendert als
    Beleg fuer „nicht unterstuetztes Format, unabhaengig von der Groesse".
    """
    from backend.config import get_settings
    from backend.db import get_sessionmaker
    from backend.vault.models import Job

    altes_word_format = b"\xd0\xcf\x11\xe0" + b"x" * 200

    # Klein: der bisherige, unveraenderte Weg.
    get_settings.cache_clear()
    klein = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("akte.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
        data={"mode": "anonymous"},
    )
    assert klein.status_code == 415

    # Gross: derselbe Statuscode, derselbe Hinweis, keine Auftragszeile.
    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    with get_sessionmaker()() as s:
        vorher = s.query(Job).count()
    try:
        gross = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": ("akte.doc", altes_word_format, "application/msword")
            },
            data={"mode": "anonymous"},
        )
        assert gross.status_code == 415, "die Groesse darf den Statuscode nicht aendern"
        assert ".docx" in gross.json()["detail"]
        with get_sessionmaker()() as s:
            assert s.query(Job).count() == vorher
    finally:
        monkeypatch.delenv("SYNC_THRESHOLD_BYTES", raising=False)
        get_settings.cache_clear()


def test_textreiche_datei_unter_der_bytegrenze_wird_ein_auftrag(client, monkeypatch):
    """Der Kern der zweiten Schranke: viel Text in wenigen Bytes.

    Genau dieser Fall lief bis zum 2026-08-28 synchron und in den
    Gateway-Timeout. Gemessen (``scripts/schwelle_messen.py``): Dieselben
    64'107 Zeichen wiegen als txt 64 KiB, als docx 39 KiB und als xlsx
    8 KiB -- Faktor acht. Eine Arbeitsmappe von 2 MiB trug bei dieser Dichte
    ueber 17 Millionen Zeichen und war rund 15 Minuten unterwegs -- weit
    jenseits dessen, was der Balancer offen haelt.

    Die Datei hier bleibt bewusst weit UNTER ``SYNC_THRESHOLD_BYTES`` und
    ueberschreitet nur ``SYNC_THRESHOLD_CHARS``. Faellt die Zeichenpruefung
    aus dem Router heraus, wird dieser Test rot -- die Byte-Pruefung allein
    liesse die Datei durch.
    """
    from backend.config import get_settings
    from backend.jobs import warteschlange

    monkeypatch.setenv("SYNC_THRESHOLD_CHARS", "200")
    get_settings.cache_clear()
    monkeypatch.setattr(
        warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True
    )
    try:
        inhalt = ("Frau Sandra Zbinden war da. " * 40).encode("utf-8")
        assert len(inhalt) < get_settings().sync_threshold_bytes, (
            "die Datei muss unter der BYTE-Grenze bleiben, sonst prueft dieser "
            "Test die alte Schranke"
        )
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={"file": ("notiz.txt", inhalt, "text/plain")},
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 202, antwort.text
        assert antwort.json()["job_id"]
    finally:
        monkeypatch.delenv("SYNC_THRESHOLD_CHARS", raising=False)
        get_settings.cache_clear()


def test_bytearme_datei_mit_wenig_text_bleibt_synchron(client, monkeypatch):
    """Die Gegenprobe — sonst waere die Schranke nur eine teurere Absage.

    Eine Datei, die knapp unter beiden Schranken liegt, muss weiterhin
    synchron laufen. Ohne diesen Test koennte die Zeichenpruefung
    versehentlich mit ``>=`` statt ``>`` oder auf der Bytezahl statt auf der
    Zeichenzahl arbeiten, und die haeufigste Nutzung -- ein normales,
    kleines Dokument -- bekaeme ohne Not den Auftragsumweg.
    """
    from backend.config import get_settings

    monkeypatch.setenv("SYNC_THRESHOLD_CHARS", "200")
    get_settings.cache_clear()
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={"file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")},
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 200
        assert "content_base64" in antwort.json()
    finally:
        monkeypatch.delenv("SYNC_THRESHOLD_CHARS", raising=False)
        get_settings.cache_clear()


def test_die_bytegrenze_greift_weiterhin_ohne_die_datei_zu_oeffnen(client, monkeypatch):
    """Die grobe Vorpruefung bleibt die erste — und das ist kein Schoenheitsfehler.

    Eine sehr grosse Datei soll gar nicht erst durch den Adapter, und eine
    BESCHAEDIGTE Datei kann er ueberhaupt nicht oeffnen: ``extract_segments``
    wirft daran. Griffe die Zeichenpruefung zuerst, endete eine beschaedigte
    grosse Datei mit einem rohen 500 mitten in der Anfrage, statt als
    sauber gescheiterter Auftrag.

    Geprueft wird mit einem kaputten ZIP unter einem .docx-Namen: Der
    Adapter kaeme daran nicht vorbei, die Byte-Schranke schon.
    """
    from backend.config import get_settings
    from backend.jobs import warteschlange

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    monkeypatch.setattr(
        warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True
    )
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": (
                    "kaputt.docx",
                    b"PK\x03\x04 das ist kein gueltiges Paket",
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document",
                )
            },
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 202, (
            "die Byte-Schranke muss VOR dem Adapterlauf greifen — sonst wirft "
            "extract_segments und die Anfrage endet mit 500"
        )
    finally:
        monkeypatch.delenv("SYNC_THRESHOLD_BYTES", raising=False)
        get_settings.cache_clear()


def _kleines_scan_pdf() -> bytes:
    """Ein winziges gescanntes PDF: eine Bildseite ohne Textebene.

    Text wird zu einem Bild gerastert und als Bild in eine neue Seite
    gesetzt, sodass ``get_text`` leer bleibt und der PdfAdapter die Seite als
    scanbasiert erkennt. Niedrige DPI hält die Datei klar unter der
    Byte-Schwelle — nur so erreicht der Router den Zeichen-Zähl-Pfad
    (``_laeuft_als_auftrag`` ruft ``extract_segments``), um den der Test geht.
    """
    import pymupdf

    quelle = pymupdf.open()
    seite = quelle.new_page()
    seite.insert_text((72, 72), "Andrea Beispiel war anwesend.")
    pix = seite.get_pixmap(dpi=36)

    ziel = pymupdf.open()
    bildseite = ziel.new_page(width=seite.rect.width, height=seite.rect.height)
    bildseite.insert_image(bildseite.rect, pixmap=pix)
    return ziel.tobytes()


def test_gescanntes_pdf_ohne_ocr_endet_mit_415_nicht_500(client):
    """Der HTTP-Wächter: ein Scan ohne OCR scheitert sauber, nicht als 500.

    ``_laeuft_als_auftrag`` ruft ``extract_segments``, um Zeichen zu zählen.
    Für ein gescanntes PDF ohne aktivierte OCR wirft der PdfAdapter dort
    ``NutzermeldungError`` (Spec §4.3, die tragende Sicherheitsregel: ein Scan,
    den niemand lesen kann, darf nie als scheinbar sauberes, unverarbeitetes
    Dokument durchgehen). Bis dieser Zweig gefangen wurde, entwich die Ausnahme
    ungefangen an FastAPI vorbei — ein 500. Jetzt wird sie in eine 415 mit dem
    erklärenden Text übersetzt, wie der synchrone Pfad es für ein nicht
    verarbeitbares Format tut.

    OCR ist im Testbetrieb aus (``OCR_ENABLED`` ungesetzt → ``ocr_enabled``
    False), unabhängig davon, ob auf der Maschine ein ``tesseract``-Binary
    liegt — die Verfügbarkeitsprüfung verlangt beides.
    """
    scan = _kleines_scan_pdf()
    assert len(scan) < 2 * 1024 * 1024, "Fixture muss unter der Byte-Schwelle bleiben"

    antwort = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("scan.pdf", scan, "application/pdf")},
        data={"mode": "anonymous"},
    )
    assert antwort.status_code == 415, antwort.text
    assert "gescannte" in antwort.json()["detail"].lower()


# ── Die Entscheidung faellt ueber die geschaetzte DAUER ────────────────────
#
# Bis zum 2026-08-28 entschied allein die Zeichenzahl. Sie konnte nie beide
# Faelle richtig bedienen: am langsamsten Format ausgerichtet bremste sie
# `.txt` um Faktor sechs aus, am schnellsten ausgerichtet riss sie bei
# Office-Dateien die 300-s-Grenze des Balancers.


def test_geschaetzte_dauer_trifft_die_gemessenen_officewerte():
    """Der Waechter der Koeffizienten.

    Die Zahlen stammen aus einer Messreihe in Produktion (2026-08-28, drei
    Worker, vier Formate). Verrutscht einer der beiden Koeffizienten, ist
    die Schaetzung fuer genau die Formate falsch, die im Betrieb
    hochgeladen werden -- und das faellt sonst erst auf, wenn jemand einen
    ``504`` bekommt.
    """
    from backend.routers.documents import geschaetzte_dauer

    # (Segmente, Zeichen, in Produktion gemessene Sekunden)
    gemessen = [
        (214, 50_000, 91.2),   # docx
        (427, 100_000, 182.3),  # docx
        (637, 149_000, 272.1),  # docx
        (427, 100_000, 180.7),  # xlsx
        (428, 100_000, 184.1),  # pptx
    ]
    for segmente, zeichen, sekunden in gemessen:
        schaetzung = geschaetzte_dauer(segmente, zeichen)
        abweichung = abs(schaetzung - sekunden) / sekunden
        assert abweichung < 0.10, (
            f"{segmente} Segmente / {zeichen} Zeichen: geschaetzt "
            f"{schaetzung:.0f}s gegen gemessene {sekunden:.0f}s"
        )


def test_segmentzahl_treibt_die_schaetzung_nicht_die_zeichenzahl():
    """Der eigentliche Befund, als Zusicherung.

    Dieselbe Zeichenzahl in vielen kurzen Segmenten muss deutlich teurer
    geschaetzt werden als in einem langen. Genau das unterscheidet eine
    docx-Datei (427 Segmente, gemessen 182 s) von einer txt-Datei mit
    gleichem Text (ein Segment, gemessen 11 s).
    """
    from backend.routers.documents import geschaetzte_dauer

    ein_stueck = geschaetzte_dauer(1, 100_000)
    viele_stuecke = geschaetzte_dauer(427, 100_000)

    assert viele_stuecke > ein_stueck * 5, (
        "die Segmentzahl muss die Schaetzung dominieren -- sonst ist der "
        "Grund fuer diese ganze Umstellung weg"
    )


def test_grosse_textdatei_laeuft_synchron(client, monkeypatch):
    """Was die Umstellung gewinnen soll.

    Eine Textdatei mit rund 300'000 Zeichen ist EIN Segment und in
    gemessenen ~90 s fertig -- sie gehoert nicht in den Auftragspfad. Unter
    der alten Zeichenschwelle (100'000, am langsamsten Format ausgerichtet)
    wurde sie zum Hintergrundauftrag.
    """
    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        inhalt = ("Die Auskunft erteilte Frau Sandra Zbinden. " * 7_000).encode("utf-8")
        einstellungen = get_settings()
        assert len(inhalt) < einstellungen.sync_threshold_bytes
        assert len(inhalt.decode("utf-8")) > 250_000, "Testdatei zu klein"

        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={"file": ("grosse_notiz.txt", inhalt, "text/plain")},
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 200, (
            "eine grosse TEXTdatei ist ein Segment und muss synchron laufen; "
            f"Status {antwort.status_code}"
        )
    finally:
        get_settings.cache_clear()


def test_viele_kurze_segmente_werden_ein_auftrag(client, monkeypatch):
    """Die Gegenrichtung: gleiche Zeichenzahl, aber zerstueckelt.

    Dieselbe Textmenge, die als `.txt` synchron laeuft, muss als Dokument
    mit vielen kurzen Segmenten in den Auftragspfad gehen -- dort kostet je
    Segment ein eigener spaCy-Durchgang.
    """
    from backend.config import get_settings
    from backend.jobs import warteschlange
    from backend.tests.ooxml_fabrik import baue_xlsx

    monkeypatch.setattr(
        warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True
    )
    get_settings.cache_clear()
    try:
        zeilen = [["Die Auskunft erteilte Frau Sandra Zbinden am Montag."]] * 3_000
        inhalt = baue_xlsx([("Blatt1", zeilen)])
        assert len(inhalt) < get_settings().sync_threshold_bytes

        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={
                "file": (
                    "liste.xlsx",
                    inhalt,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 202, (
            "viele kurze Segmente muessen als Auftrag laufen; "
            f"Status {antwort.status_code}"
        )
    finally:
        get_settings.cache_clear()
