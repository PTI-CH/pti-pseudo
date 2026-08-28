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
        assert as_utc(job.updated_at) == vorher, (
            "ein beendeter Auftrag bleibt unangetastet"
        )


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
        # Vor dem Abraeumen festhalten: ``raeume_ab`` loescht als Massenbefehl
        # und meldet der Sitzung die getroffenen Zeilen zurueck, das Objekt ist
        # danach abgeloest — ein Zugriff auf ``job.id`` laedt dann nach und
        # scheitert.
        job_id = job.id

        assert warteschlange.raeume_ab(s) == 1
        s.commit()
        assert s.get(Job, job_id) is None


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


# ── Nachbesserungen 2026-08-26 ──────────────────────────────────────────────


def test_verwaisten_abfrage_sperrt_die_zeile_auf_postgres():
    """Dieselbe Sperre wie bei der Zuteilung, aus einem eigenen Grund.

    Zwei Worker raeumen gleichzeitig auf und finden dieselbe haengende Zeile.
    Ohne Sperre zaehlt jeder eine Freigabe darauf: Der Auftrag verbraucht
    seine drei Versuche (``MAX_FREIGABEN``) in der halben Zeit und wird
    endgueltig als gescheitert gemeldet, obwohl er nur zweimal wirklich
    unterbrochen wurde. Der Mensch liest dann „mehrfach unterbrochen und
    aufgegeben" fuer ein Dokument, dem nichts fehlte.

    Wie beim Zuteilungstest wird die kompilierte SQL geprueft, nicht ein
    Fadenlauf — die Begruendung steht dort.
    """
    from datetime import datetime, timezone

    from sqlalchemy.dialects import postgresql

    from backend.jobs.warteschlange import _verwaiste_abfrage

    grenze = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sql = str(
        _verwaiste_abfrage(grenze, True).compile(dialect=postgresql.dialect())
    ).upper()
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql

    # Und auf SQLite ausdruecklich NICHT: der Dialekt kennt es nicht, ein
    # ``with_for_update`` dort haette den Aufraeumlauf im Testbetrieb und in
    # der Ein-Knoten-Rueckfallvariante zum Absturz gebracht.
    ohne = str(_verwaiste_abfrage(grenze, False).compile()).upper()
    assert "FOR UPDATE" not in ohne


def test_ein_wartender_auftrag_verfaellt_auch_ohne_arbeiter(session_factory, mandant):
    """Die Verfallszeit gilt ab Anlage, nicht erst ab Abschluss.

    Der Fall, den die 503-Schranke NICHT deckt: Sie prueft den Dialekt, aber
    der Arbeiterstart im Lifespan kann auch an einem Importfehler oder einer
    beim Start nicht erreichbaren Datenbank scheitern. Dann nimmt ``lege_an``
    weiter Auftraege an, und niemand arbeitet sie ab.

    Ohne ``expires_at`` bei der Anlage findet ``raeume_ab`` diese Zeilen nie
    — es sucht ausschliesslich Zeilen MIT Verfallszeit. Die verschluesselte
    Eingabedatei laege dann unbegrenzt in der Datenbank.
    """
    from backend.jobs import warteschlange
    from backend.vault.models import as_utc

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()
        assert job.status == "wartend"
        assert job.expires_at is not None, (
            "ohne Verfallszeit findet raeume_ab diese Zeile nie wieder"
        )
        # ``as_utc`` wie ueberall im Baum: SQLite gibt naive Zeitstempel
        # zurueck, PostgreSQL zeitzonenbehaftete.
        assert as_utc(job.expires_at) > warteschlange._jetzt()


def test_raeume_ab_entfernt_einen_nie_abgeholten_wartenden_auftrag(
    session_factory, mandant
):
    """Die Gegenprobe dazu: abgelaufen heisst weg, samt Eingabedatei.

    Der Statusfilter in ``raeume_ab`` verschont ``laeuft`` — und darf sich
    nicht bequem auf ``fertig``/``fehler`` verengen. Genau dann naehme er
    zurueck, wofuer die Verfallszeit ab Anlage eingefuehrt wurde: eine
    wartende Zeile, die nie jemand abarbeitet. Diese Zeile hier ist deshalb
    ``wartend``, nicht ``fertig`` (das prueft
    ``test_abgelaufener_auftrag_verschwindet`` eine Etage hoeher).

    Die Verfallszeit wird nicht gesetzt, sondern nur ZURUECKDATIERT — sonst
    bliebe der Test gruen, auch wenn ``lege_an`` gar keine mehr vergibt.
    """
    from backend.jobs import warteschlange
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        assert job.status == "wartend"
        assert job.expires_at is not None, "lege_an vergibt keine Verfallszeit mehr"
        # Nicht gesetzt, sondern um die volle Aufbewahrung zurueckdatiert:
        # dieselbe Zeile, nur einen Tag spaeter betrachtet.
        job.expires_at = job.expires_at - warteschlange.AUFBEWAHRUNG * 2
        s.commit()
        job_id = job.id

    with session_factory() as s:
        assert warteschlange.raeume_ab(s) == 1
        s.commit()

    with session_factory() as s:
        assert s.get(Job, job_id) is None


def test_raeume_ab_ruehrt_einen_laufenden_auftrag_nicht_an(session_factory, mandant):
    """Die Regression, die die Verfallszeit ab Anlage erst geschaffen hat.

    Solange ``expires_at`` nur an ``fertig``/``fehler`` haftete, war ein
    Abraeumen ohne Statusfilter harmlos. Seit ``lege_an`` es bei der ANLAGE
    setzt, traegt es jede Zeile ab Sekunde eins — auch die gerade bearbeitete.
    Ein Auftrag, der 24 h nach seiner Anlage noch laeuft, verlor damit seine
    Zeile mitten im Lauf.

    Der zweite Teil ist der eigentliche Schaden und deshalb mitgeprueft:
    ``melde_fertig`` schreibt danach per ``UPDATE`` auf eine verschwundene
    Zeile, und das wirft — anders als ein DELETE, das nur warnt — einen
    ``StaleDataError``.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import AuftragsErgebnis
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        # Der Herzschlag haelt ``updated_at`` frisch: die Zeile ist NICHT
        # verwaist, sie laeuft wirklich noch. Nur die Verfallszeit ab Anlage
        # ist inzwischen verstrichen.
        job.updated_at = warteschlange._jetzt()
        job.expires_at = job.expires_at - warteschlange.AUFBEWAHRUNG * 2
        s.commit()
        job_id = job.id

        assert warteschlange.raeume_ab(s) == 0, "ein laufender Auftrag bleibt"
        s.commit()
        assert s.get(Job, job_id) is not None

        # Und der Arbeiter kann seinen Lauf zu Ende bringen.
        warteschlange.melde_fertig(s, job, AuftragsErgebnis(daten=b"Ergebnis"))
        s.commit()
        assert s.get(Job, job_id).status == "fertig"


def test_ein_nicht_unterstuetztes_format_wird_gar_nicht_erst_angelegt(
    session_factory, mandant
):
    """Dieselbe Absage wie unterhalb der Schwelle — und ohne Zeile in der DB.

    Ohne diese Pruefung antwortete dieselbe .doc je nach GROESSE
    verschieden: klein sofort 415 mit dem Hinweis „bitte als .docx
    speichern", gross erst Minuten spaeter als gescheiterter Auftrag. Und
    bis dahin laege die Datei verschluesselt in der Datenbank, fuer eine
    Verarbeitung, die von vornherein nicht gelingen konnte.

    ``.doc`` statt ``.pdf``: PDF ist seit der Textebenen-Unterstuetzung ein
    unterstuetztes Format und eignet sich nicht mehr als Beleg fuer diesen
    Fall. ``.doc`` steht weiterhin in ``adapters._PLANNED``.
    """
    from sqlalchemy import func, select

    from backend.adapters import UnsupportedFormatError
    from backend.jobs import warteschlange
    from backend.vault.models import Job

    with session_factory() as s:
        with pytest.raises(UnsupportedFormatError) as info:
            warteschlange.lege_an(
                s, tenant=mandant, aktor="tester", mode="anonymize",
                dateiname="unbekannt.doc", daten=b"\xd0\xcf\x11\xe0", parameter={},
            )
        # Der Text ist der kuratierte deutsche aus ``adapters._PLANNED`` —
        # er nennt den Ausweg, nicht nur das Problem.
        assert ".docx" in str(info.value)
        s.rollback()
        assert s.scalar(select(func.count()).select_from(Job)) == 0
