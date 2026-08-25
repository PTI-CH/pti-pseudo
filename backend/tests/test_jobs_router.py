"""Prueft die Statusabfrage und das Abholen fertiger Auftraege ueber die API.

Der heikle Teil ist die Sichtbarkeit: ein Aufrufer darf ausschliesslich seine
eigenen Auftraege sehen, geprueft gegen ``aktor`` und nicht nur gegen
``tenant_id``. Ein fremder oder unbekannter Auftrag ergibt 404, nicht 403 —
ein 403 verriete, dass es ihn gibt.

Es gibt in diesem Baum keine ``conftest.py`` — jede Testdatei bringt ihre
eigene Vorrichtung mit, wie in ``test_auswahl_uebernahme.py``.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Der Aktor, den ``core.deps.current_actor`` unter HUB_AUTH=off liefert:
#: die Middleware setzt dort ``hub_user["username"] = "dev"``, und
#: ``current_actor`` gibt den Benutzernamen ohne Praefix zurueck.
EIGENER_AKTOR = "dev"


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
    from backend.jobs import warteschlange

    get_settings.cache_clear()
    reset_engine_cache()
    # Diese Datei prueft die Statusroute und die Sichtbarkeit fertiger
    # Auftraege, nicht die Anlage-Schranke -- ``auftrag_von`` unten braucht
    # ``lege_an`` nur als Vorrichtung. Die Schranke selbst hat ihren eigenen,
    # ungepatchten Test in ``test_jobs_warteschlange.py``.
    mp.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


@pytest.fixture
def auftrag_von(client):
    """Legt einen Auftrag mit gewaehltem Aktor an und gibt ihn zurueck."""
    from backend.core.deps import get_tenant
    from backend.db import get_sessionmaker
    from backend.jobs import warteschlange

    def anlegen(aktor: str, status: str = "wartend"):
        with get_sessionmaker()() as s:
            # ``get_tenant`` legt den Phase-1-Mandanten an, falls er noch
            # nicht existiert — ohne vorherigen Request gibt es hier keinen.
            tenant = get_tenant(s)
            job = warteschlange.lege_an(
                s, tenant=tenant, aktor=aktor, mode="anonymize",
                dateiname="akte.txt", daten=b"Inhalt", parameter={},
            )
            job.status = status
            s.commit()
            s.refresh(job)
            s.expunge(job)
            return job

    return anlegen


def test_status_zeigt_fortschritt_ohne_inhalte(client, auftrag_von):
    job = auftrag_von(EIGENER_AKTOR)
    antwort = client.get(f"/api/v1/jobs/{job.id}")

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["status"] in {"wartend", "laeuft", "fertig", "fehler"}
    assert "eingabe_blob" not in daten
    assert "parameter_json" not in daten


def test_fremder_auftrag_ist_nicht_auffindbar(client, auftrag_von):
    """404, nicht 403 — ein 403 verriete, dass es ihn gibt."""
    job = auftrag_von("apikey:00000000-0000-0000-0000-000000000001")
    antwort = client.get(f"/api/v1/jobs/{job.id}")
    assert antwort.status_code == 404


def test_ergebnis_vor_dem_ende_gibt_409(client, auftrag_von):
    job = auftrag_von(EIGENER_AKTOR, status="laeuft")
    antwort = client.get(f"/api/v1/jobs/{job.id}/result")
    assert antwort.status_code == 409
    assert antwort.json()["detail"]["status"] == "laeuft"


def test_ergebnis_traegt_befunde_und_tresor_kennung(client):
    """Der Auftragspfad muss dieselbe Form liefern wie der synchrone Pfad.

    Ohne Fundliste kann der Mensch bei grossen Dateien keinen Fehlalarm
    abwaehlen — die Kernsicherung dieses Werkzeugs entfiele ersatzlos. Ohne
    ``vault_id`` ist ein umkehrbar verarbeitetes Dokument nie wieder
    rueckfuehrbar: die Zuordnung Original->Pseudonym liegt verschluesselt in
    der Datenbank und ist ohne die Kennung unerreichbar.
    """
    from backend.core.deps import get_tenant
    from backend.db import get_sessionmaker
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    # Dieselbe Testzeile wie in ``test_jobs_ausfuehrung.py``: "Nadia Wenger"
    # ist frei erfunden und wird von der lokalen Erkennung vollstaendig als
    # ein einzelner PERSON-Fund erkannt — ohne Netzverkehr an die KI-Schicht.
    text = "Die Auskunft erteilte Frau Nadia Wenger am Montag.".encode("utf-8")
    with get_sessionmaker()() as s:
        tenant = get_tenant(s)
        job = warteschlange.lege_an(
            s, tenant=tenant, aktor=EIGENER_AKTOR, mode="anonymize",
            dateiname="akte.txt", daten=text,
            parameter={"modus": "reversible", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()

        ergebnis = fuehre_aus(job, text, lambda a, b: None)
        warteschlange.melde_fertig(s, job, ergebnis)
        s.commit()
        job_id = str(job.id)

    antwort = client.get(f"/api/v1/jobs/{job_id}/result")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["entities"], "die Fundliste darf nicht fehlen"
    assert daten["applied"] == 1
    assert daten["vault_id"], "ohne vault_id ist das Dokument nie rueckfuehrbar"
