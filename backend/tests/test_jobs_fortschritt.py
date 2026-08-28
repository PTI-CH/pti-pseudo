"""Tests der Fortschrittszaehlung eines Auftrags.

Bis zum 2026-08-28 gab es den Rueckruf ``melde_fortschritt`` zwar auf dem
ganzen Weg vom Arbeiter bis in die Pipeline, aber **niemand rief ihn**: Die
erste Fassung wartete auf eine Seitenzahl, die kein unterstuetztes Format
liefert (Seiten entstehen in docx und xlsx erst beim Setzen). ``fortschritt``
und ``gesamt`` standen damit fuer jeden Auftrag auf ``null``, und ein grosser
Lauf war zehn Minuten lang von einem haengengebliebenen nicht zu
unterscheiden — genau der Zustand, gegen den der Auftragspfad gebaut wurde.

Gezaehlt wird seither das **Segment**. Diese Datei sichert die drei Stellen,
an denen das schiefgehen kann:

1. Die Pipeline zaehlt vollstaendig und monoton — auch ueber leere Segmente.
2. Der Arbeiter drosselt das Schreiben, laesst den letzten Stand aber immer
   durch.
3. **Die Verdrahtung.** Ein Test, der ``melde_fortschritt`` direkt aufruft,
   bliebe gruen, waehrend ihn niemand ruft — derselbe Fehler, der die
   Zaehlung ueberhaupt erst monatelang tot liess. Geprueft wird deshalb am
   Ende, dass nach einem echten Durchgang Zahlen in der Auftragszeile stehen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters.base import Segment  # noqa: E402
from backend.vault.models import Base, Job, Tenant  # noqa: E402


def segment(id_: str, text: str) -> Segment:
    return Segment(id=id_, text=text, location=None)


# ── 1. Die Pipeline zaehlt ─────────────────────────────────────────────────


def test_jedes_segment_wird_gemeldet_auch_das_leere(monkeypatch):
    """Vollstaendig und monoton — sonst haengt der Zaehler ohne Grund.

    Das leere Segment ist der Punkt: Wuerde die Meldung erst NACH dem
    ``continue`` fuer Leersegmente stehen, bliebe der Zaehler in einem
    Dokument mit vielen Leerabsaetzen stehen, obwohl der Lauf vorankommt —
    und ein Dokument, dessen letzte Segmente alle leer sind, erreichte
    ``gesamt`` nie. In der Oberflaeche saehe beides wie ein Stillstand aus.
    """
    from backend import pipeline
    from backend.engine import analyzer as engine

    # Das Sprachmodell hat mit dem Zaehlen nichts zu tun, und es zu laden
    # machte diesen Test um Sekunden langsamer und um eine Fehlerquelle
    # reicher.
    monkeypatch.setattr(engine, "analyze", lambda **_: [])

    segmente = [
        segment("p0", "Andrea Beispiel war anwesend."),
        segment("p1", "   "),
        segment("p2", ""),
        segment("p3", "Marco Lüthi hat unterschrieben."),
    ]
    gemeldet: list[tuple[int, int]] = []

    pipeline.detect_in_segments(
        segmente, melde_fortschritt=lambda e, g: gemeldet.append((e, g))
    )

    assert gemeldet == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_ohne_rueckruf_laeuft_alles_wie_bisher(monkeypatch):
    """Der Rueckruf ist optional — der synchrone Pfad uebergibt keinen."""
    from backend import pipeline
    from backend.engine import analyzer as engine

    monkeypatch.setattr(engine, "analyze", lambda **_: [])

    ergebnis = pipeline.detect_in_segments([segment("p0", "Text.")])

    assert ergebnis.findings == []


# ── 2. Der Arbeiter drosselt ───────────────────────────────────────────────


@pytest.fixture
def session_factory(monkeypatch):
    """In-Memory-Datenbank, geteilt ueber alle Sitzungen des Tests."""
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)
    from backend.config import get_settings
    from backend.jobs import warteschlange

    get_settings.cache_clear()
    monkeypatch.setattr(
        warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True
    )
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


def _lege_auftrag_an(session_factory, mandant) -> None:
    from backend.jobs import warteschlange

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()


def test_der_zwischenstand_wird_gedrosselt_der_letzte_nie(
    session_factory, mandant, monkeypatch
):
    """Nicht zehntausend ``UPDATE`` — aber am Ende die richtige Zahl.

    Die Drosselung sitzt im Arbeiter und nicht in der Pipeline: Wer den
    Rueckruf billig beantwortet, soll ihn vollstaendig bekommen.

    Die Ausnahme fuer ``erledigt >= gesamt`` ist der eigentliche Grund
    dieses Tests. Ohne sie endete der Zaehler auf einem beliebigen
    Zwischenstand, und bis zum Statuswechsel auf ``fertig`` stuende in der
    Oberflaeche „Abschnitt 9'860 von 10'000" — als haenge der Lauf kurz vor
    dem Ziel. Ausgerechnet am Schluss also die irrefuehrendste Anzeige.
    """
    from backend.jobs.arbeiter import Arbeiter
    from backend.jobs.ausfuehrung import AuftragsErgebnis

    _lege_auftrag_an(session_factory, mandant)

    # Die Uhr steht still: So ist der Zwischenstand garantiert innerhalb des
    # Mindestabstands, ohne dass der Test von seiner eigenen Laufzeit abhinge.
    monkeypatch.setattr("backend.jobs.arbeiter.time.monotonic", lambda: 1000.0)

    def ausfuehren(job, daten, melde_fortschritt):
        melde_fortschritt(1, 3)  # der erste geht immer durch
        melde_fortschritt(2, 3)  # gedrosselt
        melde_fortschritt(3, 3)  # der letzte wieder nicht
        return AuftragsErgebnis(daten=b"fertig")

    Arbeiter(session_factory, ausfuehren)._ein_durchgang()

    with session_factory() as s:
        job = s.query(Job).one()
        assert (job.fortschritt, job.gesamt) == (3, 3)


def test_nach_dem_mindestabstand_wird_wieder_geschrieben(
    session_factory, mandant, monkeypatch
):
    """Ein langer Lauf steht nicht auf dem ersten Stand fest."""
    from backend.jobs import arbeiter as arbeiter_modul
    from backend.jobs.arbeiter import Arbeiter
    from backend.jobs.ausfuehrung import AuftragsErgebnis

    _lege_auftrag_an(session_factory, mandant)

    uhr = [1000.0]
    monkeypatch.setattr("backend.jobs.arbeiter.time.monotonic", lambda: uhr[0])

    staende: list[tuple[int | None, int | None]] = []

    def ausfuehren(job, daten, melde_fortschritt):
        melde_fortschritt(1, 100)
        staende.append((job.fortschritt, job.gesamt))
        uhr[0] += arbeiter_modul.FORTSCHRITT_TAKT_SEKUNDEN
        melde_fortschritt(50, 100)
        staende.append((job.fortschritt, job.gesamt))
        return AuftragsErgebnis(daten=b"fertig")

    Arbeiter(session_factory, ausfuehren)._ein_durchgang()

    assert staende == [(1, 100), (50, 100)]


# ── 3. Die Verdrahtung ─────────────────────────────────────────────────────


def test_ein_echter_lauf_hinterlaesst_zahlen_in_der_auftragszeile(
    session_factory, mandant, monkeypatch
):
    """Der Waechter gegen genau den Fehler, der die Zaehlung tot liess.

    Geprueft wird die ganze Kette — ``arbeiter`` -> ``ausfuehrung.fuehre_aus``
    -> ``pipeline.anonymize_document`` -> ``detect_in_segments`` -> zurueck in
    die Auftragszeile. Faellt irgendwo unterwegs die Weitergabe des Rueckrufs
    heraus, stehen hier wieder ``None``-Werte, und der Test sagt es. Ein Test
    auf den Rueckruf allein taete das nicht: Der war zwei Zyklen lang
    vorhanden und funktionsfaehig, nur eben nicht angeschlossen.
    """
    from backend.engine import analyzer as engine
    from backend.jobs import ausfuehrung
    from backend.jobs.arbeiter import Arbeiter

    monkeypatch.setattr(engine, "analyze", lambda **_: [])
    # ``fuehre_aus`` oeffnet seine eigene Sitzung ueber die globale
    # Sessionmaker-Fabrik; im Test zeigt die auf dieselbe Datenbank.
    monkeypatch.setattr(ausfuehrung, "get_sessionmaker", lambda: session_factory)

    from backend.jobs import warteschlange

    inhalt = "Andrea Beispiel war anwesend.\n\nMarco Lüthi hat unterschrieben.\n"
    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=inhalt.encode("utf-8"),
            parameter={"modus": "anonymous"},
        )
        s.commit()

    Arbeiter(session_factory, ausfuehrung.fuehre_aus)._ein_durchgang()

    with session_factory() as s:
        job = s.query(Job).one()
        assert job.status == "fertig", job.fehler
        assert job.gesamt is not None and job.gesamt > 0
        assert job.fortschritt == job.gesamt
