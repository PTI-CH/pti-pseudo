import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.vault.models import Base, Tenant


@pytest.fixture
def session_factory(monkeypatch):
    """In-Memory-Datenbank, geteilt ueber alle Sitzungen des Tests.

    Taeuscht ``hintergrundverarbeitung_moeglich`` als verfuegbar vor: Diese
    Datei prueft die Arbeiterschleife, nicht die Anlage-Schranke -- ``lege_an``
    dient hier nur als Vorrichtung, um einen Auftrag fuer die eigentliche
    Pruefung anzulegen. Die Schranke selbst hat ihren eigenen, ungepatchten
    Test in ``test_jobs_warteschlange.py``.
    """
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)
    from backend.config import get_settings
    from backend.jobs import warteschlange

    get_settings.cache_clear()
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    # StaticPool: alle Sitzungen teilen sich dieselbe In-Memory-Datenbank.
    # Ohne ihn bekaeme jede Verbindung ihre eigene, leere — und der Test
    # "zwei Arbeiter, ein Auftrag" pruefte nichts.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def test_ein_durchgang_verarbeitet_genau_einen_auftrag(session_factory, mandant):
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter
    from backend.jobs.ausfuehrung import AuftragsErgebnis

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()

    gesehen = []

    def ausfuehren(job, daten, melde_fortschritt):
        gesehen.append((job.mode, daten))
        return AuftragsErgebnis(daten=b"fertiges Dokument")

    arbeiter = Arbeiter(session_factory, ausfuehren)
    assert arbeiter._ein_durchgang() is True
    assert gesehen == [("anonymize", b"Inhalt")]
    assert arbeiter._ein_durchgang() is False, "danach ist nichts mehr da"


def test_lang_laufender_auftrag_gilt_nicht_als_verwaist(
    session_factory, mandant, monkeypatch
):
    """Waechter fuer Fund 2: der Herzschlag muss waehrend der Verarbeitung ticken.

    Setzt den Herzschlag-Takt stark herunter, statt zehn Minuten auf die
    echte Verwaisungsfrist zu warten, und laesst die (simulierte)
    Verarbeitung laenger dauern als mehrere Takte. Bewusst KEIN zweiter
    Faden, der waehrenddessen selbst an der Datenbank ruettelt: Die
    Testsuite laeuft auf SQLite mit einer geteilten StaticPool-Verbindung,
    die zwei echte, gleichzeitig zugreifende Faeden zuverlaessig zerlegt
    (siehe Kommentar zu ``hole_naechsten`` in ``warteschlange.py`` -- dort
    aus demselben Grund bewusst kein fadenbasierter Test). Waehrend
    ``ausfuehren`` blockiert (hier: schlaeft), ist der einzige Faden, der die
    Datenbank beruehrt, der Herzschlag selbst -- sequenziell, ein Takt nach
    dem anderen. Bewiesen wird die Wirkung ueber eine Spionin auf
    ``warteschlange.schlage_herz``: Sie muss waehrend der Verarbeitung
    mehrfach aufgerufen worden sein. Dass ein frischer Herzschlag
    ``gib_verwaiste_frei`` tatsaechlich abhaelt, zeigt bereits
    ``test_herzschlag_haelt_einen_alten_auftrag_von_der_verwaisung_ab`` in
    ``test_jobs_warteschlange.py`` -- zusammen ergeben beide Tests den vollen
    Beweis, ohne die riskante Nebenlaeufigkeit einzugehen.
    """
    import time

    from backend.jobs import arbeiter as arbeiter_modul
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter
    from backend.jobs.ausfuehrung import AuftragsErgebnis
    from backend.vault.models import Job

    monkeypatch.setattr(arbeiter_modul, "HERZSCHLAG_SEKUNDEN", 0.1)

    takte = []
    echter_schlag = warteschlange.schlage_herz

    def schlag_spion(session, job_id):
        takte.append(job_id)
        echter_schlag(session, job_id)

    monkeypatch.setattr(warteschlange, "schlage_herz", schlag_spion)

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()
        job_id = job.id

    def langsam(job, daten, melde_fortschritt):
        # Deutlich laenger als mehrere Herzschlag-Takte (0.1 s) -- ohne den
        # Takt wuerde in Produktion genau hier die Verwaisungsfrist reissen.
        time.sleep(0.45)
        return AuftragsErgebnis(daten=b"fertiges Dokument")

    worker = Arbeiter(session_factory, langsam)
    assert worker._ein_durchgang() is True

    assert len(takte) >= 2, "der Herzschlag haette mehrfach ticken muessen"
    assert all(t == job_id for t in takte)

    with session_factory() as s:
        fertig = s.get(Job, job_id)
        assert fertig.status == "fertig"
        assert fertig.freigaben == 0, "ohne Herzschlag waere er verwaist worden"


def test_herzschlag_endet_auch_wenn_die_verarbeitung_scheitert(
    session_factory, mandant, monkeypatch
):
    """Der Herzschlag-Faden darf einen abgebrochenen Durchgang nicht ueberleben.

    Ohne das ``finally`` um ``herzschlag_stopp()`` liefe der Herzschlag-Faden
    nach einer gescheiterten Verarbeitung munter weiter und wuerde
    irgendwann gegen einen laengst per ``melde_fehler`` beendeten (und
    damit erneut zuteilbaren) Auftrag schreiben.
    """
    from backend.jobs import arbeiter as arbeiter_modul
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter

    monkeypatch.setattr(arbeiter_modul, "HERZSCHLAG_SEKUNDEN", 0.05)

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()

    def platzt(job, daten, melde_fortschritt):
        raise RuntimeError("Format nicht unterstuetzt")

    worker = Arbeiter(session_factory, platzt)
    worker._ein_durchgang()

    laufende_herzschlag_faeden = [
        f for f in threading.enumerate() if f.name == "pti-auftraege-herzschlag"
    ]
    assert laufende_herzschlag_faeden == [], (
        "kein Herzschlag-Faden darf nach einer gescheiterten Verarbeitung "
        "noch laufen"
    )


def test_entschluesselungsfehler_scheitert_sofort_verstaendlich(
    session_factory, mandant
):
    """Waechter fuer Fund 4: ein kaputter Blob darf nicht drei Verwaisungs-
    zyklen lang haengen bleiben.

    Vorher stand ``crypto.open_bytes`` ausserhalb des geschuetzten Bereichs:
    die Ausnahme flog bis in ``_schleife`` durch, und der Auftrag blieb auf
    ``laeuft`` stehen -- er waere erst nach dreimal zehn Minuten mit der
    irrefuehrenden Meldung "mehrfach unterbrochen und aufgegeben" gescheitert
    statt sofort mit der wahren Ursache.
    """
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        job.eingabe_blob = b"zu kurz"  # kein gueltiger Nonce+Ciphertext+Tag-Blob
        s.commit()
        job_id = job.id

    def darf_nicht_laufen(job, daten, melde_fortschritt):
        raise AssertionError("ausfuehren darf bei kaputtem Blob nie erreicht werden")

    worker = Arbeiter(session_factory, darf_nicht_laufen)
    assert worker._ein_durchgang() is True

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status == "fehler"
        assert "mehrfach unterbrochen" not in job.fehler, (
            "die wahre Ursache muss stehen, nicht die Verwaisungs-Meldung"
        )
        assert job.eingabe_blob is None
        assert job.freigaben == 0, "er scheitert sofort, nicht erst nach Verwaisungen"


def test_ausnahme_im_lauf_wird_zum_fehlerstatus(session_factory, mandant):
    """Ein Absturz in der Verarbeitung darf den Arbeiter nicht mitnehmen."""
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()
        job_id = job.id

    def platzt(job, daten, melde_fortschritt):
        raise RuntimeError("Format nicht unterstuetzt")

    Arbeiter(session_factory, platzt)._ein_durchgang()

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status == "fehler"
        assert "Format nicht unterstuetzt" in job.fehler
        assert job.eingabe_blob is None
