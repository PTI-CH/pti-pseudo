import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.vault.models import Base, Tenant


@pytest.fixture
def session_factory(monkeypatch):
    """In-Memory-Datenbank, geteilt ueber alle Sitzungen des Tests.

    Taeuscht ``hintergrundverarbeitung_moeglich`` als verfuegbar vor: Diese
    Datei benutzt ``lege_an`` in fast jedem Test nur als Vorrichtung, um
    einen Auftrag fuer eine ANDERE Pruefung anzulegen (Zuteilung,
    Verschluesselung, Verwaisung, Ablauf) — die neue Dialekt-Schranke in
    ``lege_an`` selbst waere hier ein Testfehler, keine echte Ablehnung.
    Die Schranke bekommt ihren eigenen, UNGEPATCHTEN Test weiter unten
    (``test_lege_an_weist_ohne_hintergrundverarbeitung_ab``), der bewusst
    nicht diese Fixture benutzt.
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


def test_zuteilung_sperrt_die_zeile_und_ueberspringt_gesperrte():
    """Bewacht den PRODUKTIONSPFAD — den einzigen, der zaehlt.

    In Produktion laeuft PostgreSQL. Faellt dort ``SKIP LOCKED`` weg, wartet
    der zweite Worker auf die gesperrte Zeile, statt sie zu ueberspringen;
    faellt ``FOR UPDATE`` weg, bekommen beide denselben Auftrag und ein
    Dokument wird doppelt verarbeitet.

    Auf SQLite ist das nicht pruefbar — der Dialekt kennt beides nicht, und
    die Testsuite laeuft darauf. Ein fadenbasierter Test waere hier ohnehin
    das falsche Werkzeug: Er bewachte den SQLite-Zweig, der in Produktion nie
    laeuft, und waere ausserdem auf SQLite unzuverlaessig (geteilte
    ``StaticPool``-Verbindung, ``sqlite3.InterfaceError`` bei echten Faeden).
    Deshalb wird hier die von :func:`backend.jobs.warteschlange._wartende_abfrage`
    erzeugte SQL gegen den PostgreSQL-Dialekt kompiliert statt sie
    auszufuehren — dieselbe Funktion, die auch ``hole_naechsten`` benutzt,
    nicht eine nachgebaute Kopie.
    """
    from sqlalchemy.dialects import postgresql

    from backend.jobs.warteschlange import _wartende_abfrage

    sql = str(_wartende_abfrage().compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql


def test_hole_naechsten_gibt_kein_zweites_mal_denselben_auftrag(
    session_factory, mandant
):
    """Prueft den Vertrag, nicht die Nebenlaeufigkeit.

    Am 2026-08-25 gemessen: Sequenziell aufgerufen bleibt dieser Test auch
    dann gruen, wenn die Bedingung ``status == "wartend"`` im UPDATE fehlt —
    der zweite Aufruf sieht die noch nicht committete Aenderung des ersten
    bereits, weil beide Sitzungen ueber ``StaticPool`` dieselbe zugrunde
    liegende SQLite-Verbindung teilen. Was dieser Test also wirklich prueft:
    ``lege_an`` legt genau einen wartenden Auftrag an, und ``hole_naechsten``
    liefert einen bereits zugeteilten Auftrag beim naechsten Aufruf nicht
    noch einmal. Die Nebenlaeufigkeit bewacht
    ``test_zuteilung_sperrt_die_zeile_und_ueberspringt_gesperrte``.
    """
    from backend.jobs import warteschlange

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()

    with session_factory() as a, session_factory() as b:
        erster = warteschlange.hole_naechsten(a)
        zweiter = warteschlange.hole_naechsten(b)

    assert erster is not None
    assert erster.status == "laeuft"
    assert zweiter is None


def test_die_eingabe_liegt_verschluesselt(session_factory, mandant):
    """Der Klartext darf die Tabelle nie erreichen."""
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Andrea Beispiel wohnt in Thun",
            parameter={},
        )
        s.commit()
        assert b"Andrea" not in job.eingabe_blob
        assert b"Thun" not in job.eingabe_blob


def test_fertig_loescht_die_eingabe(session_factory, mandant):
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import AuftragsErgebnis

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()
        warteschlange.melde_fertig(s, job, AuftragsErgebnis(daten=b"Ergebnis"))
        s.commit()

        assert job.status == "fertig"
        assert job.eingabe_blob is None, "die Eingabe muss weg sein"
        assert job.ergebnis_blob is not None
        assert job.expires_at is not None


def test_fehler_loescht_die_eingabe_ebenfalls(session_factory, mandant):
    """Auch der gescheiterte Auftrag darf keine Personendaten liegenlassen."""
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()
        warteschlange.melde_fehler(s, job, "Format nicht unterstuetzt")
        s.commit()

        assert job.status == "fehler"
        assert job.eingabe_blob is None
        assert job.fehler == "Format nicht unterstuetzt"


def test_verwaister_auftrag_wird_wieder_freigegeben(session_factory, mandant):
    """Ein Deploy mitten im Lauf darf den Auftrag nicht fuer immer blockieren."""
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        s.commit()

        assert warteschlange.gib_verwaiste_frei(s) == 1
        s.commit()
        s.refresh(job)
        assert job.status == "wartend"
        assert job.freigaben == 1


def test_dreimal_verwaist_gilt_als_gescheitert(session_factory, mandant):
    """Sonst wandert ein Dokument, das den Arbeiter umbringt, ewig im Kreis."""
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        job.freigaben = 2
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        s.commit()

        warteschlange.gib_verwaiste_frei(s)
        s.commit()
        s.refresh(job)
        assert job.status == "fehler"
        assert job.eingabe_blob is None, "auch hier muss die Eingabe weg sein"


def test_herzschlag_haelt_einen_alten_auftrag_von_der_verwaisung_ab(
    session_factory, mandant
):
    """Waechter fuer Fund 2 auf der Datenbankebene.

    ``schlage_herz`` ist genau das, was der Arbeiter waehrend einer langen
    Verarbeitung periodisch aufruft (siehe ``backend/jobs/arbeiter.py``).
    Dieser Test datiert ``updated_at`` so weit zurueck, dass der Auftrag ohne
    Herzschlag laengst verwaist waere, ruft dann genau einmal ``schlage_herz``
    -- und zeigt, dass ``gib_verwaiste_frei`` ihn danach in Ruhe laesst.
    """
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        s.commit()
        job_id = job.id

        warteschlange.schlage_herz(s, job_id)
        s.commit()

        assert warteschlange.gib_verwaiste_frei(s) == 0, (
            "der frische Herzschlag haette die Verwaisung verhindern muessen"
        )
        s.commit()
        s.refresh(job)
        assert job.status == "laeuft"
        assert job.freigaben == 0


def test_herzschlag_laesst_einen_bereits_beendeten_auftrag_in_ruhe(
    session_factory, mandant
):
    """Ein Herzschlag nach dem Ende darf einen abgeschlossenen Auftrag nicht wiederbeleben.

    Deckt die Bedingung ``status == "laeuft"`` in ``schlage_herz`` ab: ohne
    sie koennte ein letzter, spaeter Takt einen laengst fertigen oder
    gescheiterten Auftrag erneut wie laufend aussehen lassen.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import AuftragsErgebnis
    from backend.vault.models import as_utc

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()
        job_id = job.id

        warteschlange.melde_fertig(s, job, AuftragsErgebnis(daten=b"Ergebnis"))
        vorher = as_utc(job.updated_at)
        s.commit()

        warteschlange.schlage_herz(s, job_id)
        s.commit()
        s.refresh(job)
        assert as_utc(job.updated_at) == vorher, "ein beendeter Auftrag bleibt unangetastet"


def test_abgelaufener_auftrag_verschwindet(session_factory, mandant):
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import AuftragsErgebnis
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        warteschlange.melde_fertig(s, job, AuftragsErgebnis(daten=b"Ergebnis"))
        job.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        s.commit()

        assert warteschlange.raeume_ab(s) == 1
        s.commit()
        assert s.get(Job, job.id) is None


def test_hintergrundverarbeitung_moeglich_nur_auf_postgresql():
    """Reine Funktionspruefung, keine Datenbank noetig.

    Dieselbe Bedingung entscheidet den Arbeiterstart in ``main.py`` und die
    Ablehnung in ``lege_an`` — beide rufen diese eine Funktion auf, damit sie
    nicht auseinanderlaufen koennen (siehe Docstring dort).
    """
    from backend.jobs.warteschlange import hintergrundverarbeitung_moeglich

    assert hintergrundverarbeitung_moeglich("postgresql") is True
    assert hintergrundverarbeitung_moeglich("sqlite") is False
    assert hintergrundverarbeitung_moeglich("mysql") is False


def test_lege_an_weist_ohne_hintergrundverarbeitung_ab():
    """Der eigentliche Fix: kein Auftrag entsteht, den nie jemand abarbeitet.

    Bewusst OHNE die ``session_factory``-Fixture dieser Datei — die patcht
    ``hintergrundverarbeitung_moeglich`` fuer alle anderen Tests weg, weil sie
    ``lege_an`` dort nur als Vorrichtung braucht. Dieser Test will genau das
    Gegenteil pruefen: das eigene, ungepatchte Verhalten von ``lege_an`` auf
    einem Dialekt, der den Arbeiter nie startet (hier: SQLite, wie im echten
    Testbetrieb und in der Ein-Knoten-Rueckfallvariante, ``JELASTIC_SETUP.md``
    1b). Vorher blieb ein solcher Auftrag fuer immer auf ``wartend`` stehen;
    jetzt entsteht er gar nicht erst.
    """
    import pytest as _pytest

    from backend.jobs import warteschlange
    from backend.jobs.warteschlange import AuftragNichtMoeglichError
    from backend.vault.models import Job

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Sitzung = sessionmaker(bind=engine)

    with Sitzung() as s:
        tenant = Tenant(name="Testmandant")
        s.add(tenant)
        s.commit()
        s.refresh(tenant)

        with _pytest.raises(AuftragNichtMoeglichError):
            warteschlange.lege_an(
                s, tenant=tenant, aktor="benutzer:tester", mode="anonymize",
                dateiname="akte.docx", daten=b"Inhalt", parameter={},
            )
        s.rollback()

        # Der wichtigere Teil der Zusicherung: keine Muellzeile.
        assert s.query(Job).count() == 0
