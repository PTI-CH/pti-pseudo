"""Der Aufraeumlauf beim Prozessstart — und warum er nicht im Arbeiter reicht.

Der Aufraeumlauf lebte ausschliesslich im Arbeiterfaden (jeder 150. Durchgang).
Startet der Arbeiter nicht, raeumt also niemand. Die 503-Schranke in
``warteschlange.lege_an`` deckt den HAEUFIGEN Grund ab -- den falschen
Datenbankdialekt, dann entsteht gar keine Auftragszeile. Sie deckt NICHT den
seltenen ab, gegen den im Lifespan ein ``except`` steht: ein Importfehler oder
eine beim Start nicht erreichbare Datenbank. Dann nimmt ``lege_an`` weiterhin
Auftraege an, und jede angenommene Zeile traegt die verschluesselte
Eingabedatei mit sich -- unbegrenzt.

Zusammen mit der Verfallszeit ab Anlage (``lege_an``) schliesst der Startlauf
die Luecke: Jeder Prozessstart raeumt nach, und ein Neustart kommt auf dem
Knoten spaetestens mit dem naechsten Deploy.

**Die erste Zusicherung hier ist die Verdrahtung selbst.** Alle weiteren
pruefen ``warteschlange.aufraeumlauf`` direkt — das ist bequem und
aussagearm, solange niemand nachweist, dass der Lifespan sie ueberhaupt ruft.
Genau dieser Fehler ist in diesem Projekt schon einmal passiert (siehe
``test_protokoll.py``: die Protokollkonfiguration war korrekt und wurde
nirgends aufgerufen, ein halbes Jahr lang).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.vault.models import Base, Job, Tenant


@pytest.fixture
def session_factory(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)
    from backend.config import get_settings

    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    get_settings.cache_clear()


@pytest.fixture
def mandant(session_factory):
    with session_factory() as s:
        t = Tenant(name="Testmandant")
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def test_der_lifespan_ruft_den_aufraeumlauf_beim_start(monkeypatch):
    """Die Verdrahtung — die einzige Zusicherung, auf die es hier ankommt.

    Alle anderen Tests dieser Datei rufen ``aufraeumlauf`` selbst auf. Sie
    blieben vollstaendig gruen, wenn der Aufruf im Lifespan wirkungslos waere
    (nachgemessen); getestet waere dann ein Helfer, zugesichert eine
    Verdrahtung. Deshalb faehrt dieser Test die Anwendung wirklich hoch: Der
    ``TestClient`` als Kontextmanager ist das, was den ``lifespan`` durchlaeuft
    — ``create_app()`` allein tut es nicht.

    Das spaCy-Modell wird dabei ausgehaengt: Es kostet sieben Sekunden und
    hat mit dieser Zusicherung nichts zu tun. Der Aufraeumlauf selbst wird
    nicht nachgebaut, sondern nur mitgeschrieben.
    """
    from fastapi.testclient import TestClient

    for name, wert in (
        ("HUB_AUTH", "off"), ("ENV", "development"),
        ("DATABASE_URL", "sqlite://"), ("VAULT_MASTER_KEY", "e" * 64),
    ):
        monkeypatch.setenv(name, wert)

    from backend.config import get_settings
    from backend.db import reset_engine_cache
    from backend.engine import analyzer
    from backend.jobs import warteschlange

    get_settings.cache_clear()
    reset_engine_cache()

    monkeypatch.setattr(analyzer, "get_default_analyzer", lambda *a, **k: None)

    gerufen: list[Session] = []

    def mitschreiben(session):
        gerufen.append(session)
        return 0

    monkeypatch.setattr(warteschlange, "aufraeumlauf", mitschreiben)

    from backend.main import create_app

    try:
        with TestClient(create_app()):
            pass
    finally:
        get_settings.cache_clear()
        reset_engine_cache()

    assert len(gerufen) == 1, "der Start raeumt genau einmal auf"
    assert isinstance(gerufen[0], Session), "und zwar mit einer echten Sitzung"


def test_der_startlauf_entfernt_eine_liegengebliebene_eingabedatei(
    session_factory, mandant
):
    """Der Fall, den es ohne Arbeiter sonst nie gaebe.

    Eine wartende Zeile mit abgelaufener Verfallszeit — also eine, die
    angenommen, aber nie abgearbeitet wurde. Ohne den Startlauf bliebe sie
    samt verschluesselter Eingabedatei fuer immer liegen.
    """
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = Job(
            tenant_id=mandant.id, mode="anonymize", status="wartend",
            eingabe_blob=b"verschluesselte Eingabe",
            expires_at=warteschlange._jetzt() - warteschlange.AUFBEWAHRUNG,
            updated_at=warteschlange._jetzt(),
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with session_factory() as s:
        assert warteschlange.aufraeumlauf(s) == 1

    with session_factory() as s:
        assert s.get(Job, job_id) is None


def test_der_startlauf_gibt_einen_verwaisten_auftrag_frei(session_factory, mandant):
    """Eine ``laeuft``-Zeile wird ueberhaupt erst durch die Freigabe abraeumbar.

    Die Begruendung ist ausdruecklich NICHT der Deploy: Danach ist
    ``updated_at`` solcher Zeilen frisch, die Verwaisungsfrist also gar nicht
    gerissen — und selbst wenn, faende der frisch gestartete Arbeiter sie in
    seinem allerersten Durchgang, zwei Sekunden spaeter. Der Startlauf traegt
    hier denselben Fall wie beim Loeschen: Der Arbeiter startet NICHT
    (Importfehler, Datenbank beim Start nicht erreichbar), und der Prozess
    stand laenger still als ``VERWAIST_NACH`` — die Zeile hier ist deshalb
    zwanzig Minuten alt.

    Ohne die Freigabe bliebe sie fuer immer auf ``laeuft``, und ``raeume_ab``
    ruehrt ``laeuft``-Zeilen grundsaetzlich nicht an: Die verschluesselte
    Eingabedatei laege dann dauerhaft in der Datenbank, obwohl niemand mehr
    daran arbeitet. Erst die Freigabe macht sie wieder abraeumbar.
    """
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = Job(
            tenant_id=mandant.id, mode="anonymize", status="laeuft",
            eingabe_blob=b"verschluesselte Eingabe",
            expires_at=warteschlange._jetzt() + warteschlange.AUFBEWAHRUNG,
            updated_at=warteschlange._jetzt() - warteschlange.VERWAIST_NACH * 2,
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with session_factory() as s:
        warteschlange.aufraeumlauf(s)

    with session_factory() as s:
        assert s.get(Job, job_id).status == "wartend"


def test_der_startlauf_laesst_einen_gesunden_auftrag_in_ruhe(session_factory, mandant):
    """Die Gegenprobe: Er darf nicht alles wegraeumen, was er findet.

    Der laufende Auftrag hat hier die Gestalt, die ``lege_an`` seit der
    Verfallszeit-ab-Anlage tatsaechlich erzeugt — mit ``expires_at``, und zwar
    einem **bereits abgelaufenen**: Er wurde vor mehr als
    ``AUFBEWAHRUNG`` angelegt und laeuft immer noch, sein ``updated_at`` haelt
    der Herzschlag frisch. Genau diese Zeile loeschte ``raeume_ab``, solange es
    ohne Statusfilter jede abgelaufene Zeile nahm — mitten im Lauf, und das
    ``UPDATE`` aus ``melde_fertig`` scheiterte danach mit ``StaleDataError``.
    Eine Zeile mit ``expires_at = None`` (die alte Fassung dieses Tests) haette
    das nie bemerkt, weil ``raeume_ab`` sie ohnehin nicht ansieht.

    Und ohne die Zusicherung insgesamt waere die einfachste Umsetzung — alles
    loeschen — ebenfalls gruen, und ein Neustart des Prozesses vernichtete
    jeden gerade laufenden und jeden noch nicht abgeholten Auftrag. Bei zwei
    uvicorn-Workern startet der zweite waehrend der erste schon arbeitet.
    """
    from backend.jobs import warteschlange

    with session_factory() as s:
        laufend = Job(
            tenant_id=mandant.id, mode="anonymize", status="laeuft",
            eingabe_blob=b"verschluesselte Eingabe",
            expires_at=warteschlange._jetzt() - warteschlange.AUFBEWAHRUNG,
            updated_at=warteschlange._jetzt(),
        )
        fertig = Job(
            tenant_id=mandant.id, mode="anonymize", status="fertig",
            ergebnis_blob=b"Ergebnis",
            expires_at=warteschlange._jetzt() + warteschlange.AUFBEWAHRUNG,
        )
        s.add_all([laufend, fertig])
        s.commit()
        ids = (laufend.id, fertig.id)

    with session_factory() as s:
        assert warteschlange.aufraeumlauf(s) == 0

    with session_factory() as s:
        assert s.get(Job, ids[0]).status == "laeuft", "ein gesunder Lauf bleibt"
        assert s.get(Job, ids[1]) is not None, "ein abholbares Ergebnis bleibt"
