"""Tests fuer die Nutzungsauswertung je API-Schluessel (``GET /api/v1/api-keys/usage``).

Der Endpunkt beantwortet die Frage, die ``last_used_at`` nur halb beantwortet:
nicht *ob* ein Schluessel noch benutzt wird, sondern **wie stark und wofuer**.

Zwei Dinge werden hier bewusst mitgeprueft:

* **Attribution ueber die Schluessel-ID, nicht ueber die Bezeichnung.** Zwei
  Schluessel duerfen dieselbe Bezeichnung tragen (ein widerrufener wird gern
  unter gleichem Namen neu ausgestellt) — eine Auswertung nach Bezeichnung
  wuerde ihre Zahlen vermengen. Der Test legt deshalb zwei Schluessel mit
  **identischer Bezeichnung** an und verlangt getrennte Zahlen.
* **Kein zweiter Einhaengepunkt.** Wie die uebrige Schluesselverwaltung haengt
  der Endpunkt nicht unter ``/api/ext/v1`` — sonst koennte eine aufrufende
  Anwendung die Nutzungsprofile aller Schwester-Anwendungen auslesen.

Die Fixture ist wie in ``test_api_keys_admin.py`` bewusst kopiert statt
importiert: jede Testdatei soll ohne stille Kopplung an eine andere lesbar
bleiben.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Testtext mit echten Umlauten. Frei erfunden — es gehoeren keine echten
#: Personendaten in dieses Repository.
TEXT = "Herr Andreas Meier wohnt in 8001 Zürich und schreibt später erneut."


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Faehrt die Anwendung mit abgeschaltetem SSO (synthetischer Admin) hoch."""
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


@pytest.fixture
def client_standard(client: TestClient) -> Iterator[TestClient]:
    """Dieselbe Anwendung, aber der synthetische Dev-Benutzer ist kein Admin.

    Bewusst **ohne** ``reset_engine_cache``: die In-Memory-Datenbank der
    ``client``-Fixture soll erhalten bleiben.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("DEV_ROLE", "standard")

    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        from backend.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


def _erzeuge(client: TestClient, label: str) -> tuple[str, str]:
    """Legt einen Schluessel ueber den Verwaltungs-Endpunkt an: (id, token)."""
    r = client.post("/api/v1/api-keys", json={"label": label})
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["token"]


def _nutzung(client: TestClient, days: int | None = None) -> dict:
    pfad = "/api/v1/api-keys/usage"
    if days is not None:
        pfad = f"{pfad}?days={days}"
    r = client.get(pfad)
    assert r.status_code == 200, r.text
    return r.json()


def _eintrag(antwort: dict, key_id: str) -> dict:
    treffer = [k for k in antwort["keys"] if k["id"] == key_id]
    assert len(treffer) == 1, f"{key_id} kommt {len(treffer)}-mal vor"
    return treffer[0]


def _altes_ereignis(key_id: str, tage: int, char_count: int = 999) -> None:
    """Schreibt einen Nutzungseintrag mit zurueckdatiertem Zeitstempel.

    Ueber die API laesst sich kein alter Eintrag erzeugen — der Zeitfilter
    waere sonst nicht pruefbar.
    """
    from backend.core.deps import get_tenant
    from backend.db import get_sessionmaker
    from backend.vault.models import UsageEvent

    with get_sessionmaker()() as session:
        tenant = get_tenant(session)
        session.add(
            UsageEvent(
                tenant_id=tenant.id,
                username=f"apikey:{key_id}",
                action="detect",
                entity_counts={"PERSON": 7},
                char_count=char_count,
                created_at=datetime.now(timezone.utc) - timedelta(days=tage),
            )
        )
        session.commit()


# ── Attribution ueber die Schluessel-ID ─────────────────────────────────────


def test_aktor_traegt_die_schluessel_id_nicht_die_bezeichnung(
    client: TestClient,
) -> None:
    """``usage_events.username`` muss den Schluessel eindeutig benennen.

    Bezeichnungen sind nicht eindeutig — ein widerrufener Schluessel wird gern
    unter demselben Namen neu ausgestellt. Die ID ist es.
    """
    from sqlalchemy import select

    from backend.db import get_sessionmaker
    from backend.vault.models import UsageEvent

    key_id, token = _erzeuge(client, "Aktor-ID")
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": TEXT},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 200, r.text

    with get_sessionmaker()() as session:
        namen = set(session.execute(select(UsageEvent.username)).scalars().all())
    assert f"apikey:{key_id}" in namen
    assert "apikey:Aktor-ID" not in namen


# ── Kernauswertung ──────────────────────────────────────────────────────────


def test_zahlen_je_schluessel(client: TestClient) -> None:
    """Aufrufe, Aktionen, Zeichen und Kategorien je Schluessel — sauber getrennt.

    Beide Schluessel tragen bewusst dieselbe Bezeichnung: eine Auswertung nach
    Bezeichnung wuerde hier durchfallen.
    """
    a_id, a_token = _erzeuge(client, "Gleicher Name")
    b_id, b_token = _erzeuge(client, "Gleicher Name")
    ohne_id, _ = _erzeuge(client, "Nie benutzt")

    zeichen_a = 0
    for _ in range(2):
        r = client.post(
            "/api/ext/v1/documents:detect",
            json={"text": TEXT},
            headers={"X-Api-Key": a_token},
        )
        assert r.status_code == 200, r.text
        zeichen_a += r.json()["char_count"]

    anonym = client.post(
        "/api/ext/v1/documents:anonymize",
        json={"text": TEXT, "mode": "reversible", "vault_label": "Nutzung A"},
        headers={"X-Api-Key": a_token},
    )
    assert anonym.status_code == 200, anonym.text
    zeichen_a += anonym.json()["char_count"]

    zurueck = client.post(
        "/api/ext/v1/documents:reverse",
        json={
            "text": anonym.json()["text"],
            "vault_id": anonym.json()["vault_id"],
        },
        headers={"X-Api-Key": a_token},
    )
    assert zurueck.status_code == 200, zurueck.text
    zeichen_a += len(anonym.json()["text"])

    b = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": TEXT},
        headers={"X-Api-Key": b_token},
    )
    assert b.status_code == 200, b.text
    zeichen_b = b.json()["char_count"]

    antwort = _nutzung(client)
    assert antwort["days"] == 30
    assert antwort["since"] is not None

    eintrag_a = _eintrag(antwort, a_id)
    assert eintrag_a["label"] == "Gleicher Name"
    assert eintrag_a["key_prefix"]
    assert eintrag_a["active"] is True
    assert eintrag_a["requests"] == 4
    assert eintrag_a["by_action"] == {"detect": 2, "anonymize": 1, "reverse": 1}
    assert eintrag_a["char_count"] == zeichen_a
    assert eintrag_a["entity_counts"]["PERSON"] >= 3
    assert eintrag_a["first_event_at"] is not None
    assert eintrag_a["last_event_at"] is not None

    eintrag_b = _eintrag(antwort, b_id)
    assert eintrag_b["requests"] == 1
    assert eintrag_b["by_action"] == {"detect": 1}
    assert eintrag_b["char_count"] == zeichen_b

    # Ein nie benutzter Schluessel fehlt nicht, er steht mit Nullen da.
    eintrag_ohne = _eintrag(antwort, ohne_id)
    assert eintrag_ohne["requests"] == 0
    assert eintrag_ohne["char_count"] == 0
    assert eintrag_ohne["by_action"] == {}
    assert eintrag_ohne["entity_counts"] == {}
    assert eintrag_ohne["daily"] == []
    assert eintrag_ohne["first_event_at"] is None
    assert eintrag_ohne["last_event_at"] is None

    # Sortierung: viel benutzt vor wenig benutzt vor gar nicht benutzt.
    reihenfolge = [k["id"] for k in antwort["keys"]]
    assert reihenfolge.index(a_id) < reihenfolge.index(b_id)
    assert reihenfolge.index(b_id) < reihenfolge.index(ohne_id)


def test_tagesreihe_nur_tage_mit_ereignissen_aufsteigend(client: TestClient) -> None:
    """``daily`` traegt die Balken der Oberflaeche — Luecken werden nicht gefuellt."""
    key_id, token = _erzeuge(client, "Tagesreihe")
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": TEXT},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 200, r.text
    _altes_ereignis(key_id, tage=3, char_count=100)

    eintrag = _eintrag(_nutzung(client), key_id)
    assert len(eintrag["daily"]) == 2
    datumsliste = [t["date"] for t in eintrag["daily"]]
    assert datumsliste == sorted(datumsliste), "aufsteigend, nicht umgekehrt"
    assert all(len(t["date"]) == 10 for t in eintrag["daily"])
    assert sum(t["requests"] for t in eintrag["daily"]) == eintrag["requests"]
    assert sum(t["char_count"] for t in eintrag["daily"]) == eintrag["char_count"]


def test_widerrufener_schluessel_bleibt_in_der_auswertung(client: TestClient) -> None:
    """Sonst verschwaende der Widerruf die Nachvollziehbarkeit rueckwirkend."""
    key_id, token = _erzeuge(client, "Widerrufen mit Nutzung")
    assert (
        client.post(
            "/api/ext/v1/documents:detect",
            json={"text": TEXT},
            headers={"X-Api-Key": token},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/api-keys/{key_id}").status_code == 200

    eintrag = _eintrag(_nutzung(client), key_id)
    assert eintrag["active"] is False
    assert eintrag["revoked_at"] is not None
    assert eintrag["requests"] == 1


# ── Zeitfenster ─────────────────────────────────────────────────────────────


def test_days_filtert_alte_ereignisse(client: TestClient) -> None:
    """``days=30`` blendet einen 100 Tage alten Eintrag aus, ``days=0`` nicht."""
    key_id, token = _erzeuge(client, "Zeitfenster")
    assert (
        client.post(
            "/api/ext/v1/documents:detect",
            json={"text": TEXT},
            headers={"X-Api-Key": token},
        ).status_code
        == 200
    )
    _altes_ereignis(key_id, tage=100, char_count=4242)

    dreissig = _nutzung(client, days=30)
    assert dreissig["days"] == 30
    eintrag_30 = _eintrag(dreissig, key_id)
    assert eintrag_30["requests"] == 1
    assert eintrag_30["char_count"] != 4242

    alles = _nutzung(client, days=0)
    assert alles["days"] is None
    assert alles["since"] is None
    eintrag_alles = _eintrag(alles, key_id)
    assert eintrag_alles["requests"] == 2
    assert eintrag_alles["entity_counts"]["PERSON"] >= 7


def test_grenzfall_des_zeitfensters(client: TestClient) -> None:
    """Die Kante von ``days=30`` liegt zwischen 29 und 31 Tagen.

    ``test_days_filtert_alte_ereignisse`` prueft mit 100 Tagen nur, dass
    ueberhaupt gefiltert wird — es bliebe auch dann gruen, wenn die Grenze bei
    60 oder 90 Tagen laege. Erst zwei Eintraege dicht neben der Kante zeigen,
    dass sie an der richtigen Stelle sitzt.
    """
    key_id, _ = _erzeuge(client, "Kante")
    _altes_ereignis(key_id, tage=29, char_count=29)
    _altes_ereignis(key_id, tage=31, char_count=31)

    eintrag = _eintrag(_nutzung(client, days=30), key_id)
    assert eintrag["requests"] == 1
    assert eintrag["char_count"] == 29, "der 31 Tage alte Eintrag gehoert nicht dazu"

    beide = _eintrag(_nutzung(client, days=0), key_id)
    assert beide["requests"] == 2
    assert beide["char_count"] == 60


def test_sso_aufrufe_zaehlen_bei_keinem_schluessel(client: TestClient) -> None:
    """Ein Mensch am Menschenpfad darf in keiner Schluesselzeile auftauchen.

    Beide Pfade schreiben in dieselbe Tabelle. Filterte die Auswertung nicht
    auf das ``apikey:``-Praefix, landeten die Aufrufe der Oberflaeche in den
    Zahlen irgendeines Schluessels — und niemand saehe es der Tabelle an.
    """
    vorher = _nutzung(client, days=0)
    summe_vorher = sum(k["requests"] for k in vorher["keys"])

    r = client.post("/api/v1/documents:detect", json={"text": TEXT})
    assert r.status_code == 200, r.text

    nachher = _nutzung(client, days=0)
    assert sum(k["requests"] for k in nachher["keys"]) == summe_vorher

    # Gegenprobe, damit der Test nicht bloss "es passiert nichts" misst: der
    # Eintrag ist geschrieben worden, nur eben unter einem Nicht-Schluessel.
    from sqlalchemy import select

    from backend.db import get_sessionmaker
    from backend.vault.models import UsageEvent

    with get_sessionmaker()() as session:
        namen = set(session.execute(select(UsageEvent.username)).scalars().all())
    assert any(not n.startswith("apikey:") for n in namen)


def test_ungueltige_days_400(client: TestClient) -> None:
    for wert in ("-1", "3651", "99999"):
        r = client.get(f"/api/v1/api-keys/usage?days={wert}")
        assert r.status_code == 400, (wert, r.text)


def test_days_grenzen_bleiben_gueltig(client: TestClient) -> None:
    for wert in ("1", "3650"):
        r = client.get(f"/api/v1/api-keys/usage?days={wert}")
        assert r.status_code == 200, (wert, r.text)


# ── Zugang ──────────────────────────────────────────────────────────────────


def test_ohne_adminrolle_403(client_standard: TestClient) -> None:
    r = client_standard.get("/api/v1/api-keys/usage")
    assert r.status_code == 403, r.text


def test_nicht_unter_ext_eingehaengt(client: TestClient) -> None:
    """Ein Schluessel darf die Nutzungsprofile der anderen nicht lesen.

    Es ist der Einhaengepunkt, der fehlt (404), nicht die Authentisierung —
    deshalb der Aufruf mit einem gueltigen Token im Header.
    """
    _, token = _erzeuge(client, "Fuer den Mount-Test")
    r = client.get("/api/ext/v1/api-keys/usage", headers={"X-Api-Key": token})
    assert r.status_code == 404, r.text


# ── Nur Metadaten ───────────────────────────────────────────────────────────


def test_antwort_enthaelt_keine_inhalte(client: TestClient) -> None:
    """Die Auswertung ist Zahlenwerk — kein Text, kein Dateiname, kein Pseudonym.

    Geprueft am Rohtext der Antwort: ein Ausschnitt des verarbeiteten Textes
    darf dort unter keinen Umstaenden auftauchen.
    """
    _, token = _erzeuge(client, "Nur Zahlen")
    assert (
        client.post(
            "/api/ext/v1/documents:detect",
            json={"text": TEXT},
            headers={"X-Api-Key": token},
        ).status_code
        == 200
    )

    roh = client.get("/api/v1/api-keys/usage").text
    assert "Andreas" not in roh
    assert "Zürich" not in roh
