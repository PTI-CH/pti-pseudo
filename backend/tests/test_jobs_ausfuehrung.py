import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.vault.models import Base, Tenant


@pytest.fixture
def session_factory(monkeypatch):
    """In-Memory-Datenbank, geteilt ueber alle Sitzungen des Tests.

    Taeuscht ``hintergrundverarbeitung_moeglich`` als verfuegbar vor: Diese
    Datei prueft die Ausfuehrung fertig zugeteilter Auftraege, nicht die
    Anlage-Schranke -- ``lege_an`` dient hier nur als Vorrichtung, um einen
    Auftrag fuer die eigentliche Pruefung anzulegen. Die Schranke selbst hat
    ihren eigenen, ungepatchten Test in ``test_jobs_warteschlange.py``.
    """
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)
    from backend.config import get_settings
    from backend.jobs import warteschlange

    get_settings.cache_clear()
    monkeypatch.setattr(warteschlange, "hintergrundverarbeitung_moeglich", lambda *_: True)
    # StaticPool: alle Sitzungen teilen sich dieselbe In-Memory-Datenbank.
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


def test_anonymize_auftrag_liefert_ein_pseudonymisiertes_dokument(
    session_factory, mandant, monkeypatch
):
    """Der Auftrag muss dasselbe tun wie der synchrone Pfad."""
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    # ``fuehre_aus`` oeffnet seine eigene Sitzung ueber ``backend.db.
    # get_sessionmaker`` (kein ``session``-Parameter in der Schnittstelle).
    # Damit sie dieselbe In-Memory-Datenbank sieht wie diese Testvorrichtung,
    # wird genau diese Referenz im Modul umgebogen — die Vorrichtung selbst
    # bleibt unveraendert die aus ``test_jobs_warteschlange.py``.
    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    # Bewusst NICHT der Satz mit "Sandra Zbinden": der ist im Baum das
    # Beispiel des Namenssplits, den erst die KI-Schicht behebt (siehe
    # backend/ki/namen.py, test_ki_pipeline.py). Mit ki_pruefung=False
    # faende die lokale Erkennung dort nur "Sandra" und dieser Test pruefte
    # dann eine bekannte Modellschwaeche statt der Naht Auftrag-Pipeline.
    # ki_pruefung=True kommt hier ebenfalls nicht in Frage: Tests duerfen
    # keinen Netzverkehr an die Infomaniak AI Services ausloesen (siehe
    # Uebergreifende Vorgaben) — das haenge sonst daran, ob lokal zufaellig
    # ein INFOMANIAK_AI_KEY gesetzt ist. "Nadia Wenger" ist frei erfunden
    # und wird von der lokalen Erkennung nachweislich vollstaendig als ein
    # PERSON-Fund erkannt (siehe Testlauf im Aufgabenbericht).
    text = "Die Auskunft erteilte Frau Nadia Wenger am Montag.".encode("utf-8")
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=text,
            parameter={"modus": "anonymous", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()

        ergebnis = fuehre_aus(job, text, lambda a, b: None)

    # ``fuehre_aus`` liefert seit Aufgabe 9 ein ``AuftragsErgebnis`` statt
    # nackter Bytes — ``daten`` traegt das Dokument, ``zusatz`` die
    # Angaben, die die Ergebnisroute in denselben Schluesseln zeigt wie der
    # synchrone Pfad (siehe test_jobs_router.py).
    assert b"Nadia" not in ergebnis.daten, "der Vorname muss ersetzt sein"
    assert b"Wenger" not in ergebnis.daten, "der Nachname muss ersetzt sein"
    assert b"Montag" in ergebnis.daten, "der uebrige Text bleibt (nicht alles ersetzt)"
    assert ergebnis.zusatz["applied"] == 1
    assert ergebnis.zusatz["vault_id"] is None, "Modus anonymous legt keinen Vault an"


def test_unbekannter_modus_scheitert_verstaendlich(
    session_factory, mandant, monkeypatch
):
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="unfug",
            dateiname="notiz.txt", daten=b"x", parameter={},
        )
        s.commit()
        with pytest.raises(ValueError, match="unfug"):
            fuehre_aus(job, b"x", lambda a, b: None)


def test_fremder_vault_wird_abgewiesen(session_factory, mandant, monkeypatch):
    """Ein Auftrag darf nie den Tresor eines FREMDEN Mandanten benutzen.

    Ohne die Mandantenpruefung in ``_hole_vault`` liesse sich ueber ein
    manipuliertes ``vault_id`` die Zuordnung Original->Pseudonym eines
    fremden Mandanten auf das eigene Dokument anwenden — genau das soll der
    Vault verhindern (siehe ``documents._resolve_vault`` als Vorlage).
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus
    from backend.vault.models import Tenant, Vault

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    with session_factory() as s:
        fremder_mandant = Tenant(name="Fremdmandant")
        s.add(fremder_mandant)
        s.commit()
        s.refresh(fremder_mandant)

        fremder_vault = Vault(tenant_id=fremder_mandant.id, label="Fremdtresor")
        s.add(fremder_vault)
        s.commit()
        s.refresh(fremder_vault)

        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=b"Text",
            parameter={"modus": "reversible", "vault_id": str(fremder_vault.id),
                       "entities": None, "ki_pruefung": False,
                       "selected_entities": None},
        )
        s.commit()

        with pytest.raises(ValueError, match="Zuordnungstabelle nicht gefunden"):
            fuehre_aus(job, b"Text", lambda a, b: None)


def test_unbekannter_dokumentmodus_scheitert_verstaendlich(
    session_factory, mandant, monkeypatch
):
    """Ein Tippfehler im Modus darf nicht still im umkehrbaren Zweig landen.

    Ohne die Moduspruefung in ``_hole_vault`` waere ein Dokument, das der
    Mensch fuer endgueltig anonymisiert haelt, ploetzlich rueckfuehrbar — nur
    weil der Modus nicht exakt ``anonymous`` oder ``reversible`` lautete.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=b"Text",
            parameter={"modus": "anonymus", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()

        with pytest.raises(ValueError, match="Unbekannter Modus 'anonymus'"):
            fuehre_aus(job, b"Text", lambda a, b: None)


def test_reverse_fremder_vault_wird_abgewiesen(session_factory, mandant, monkeypatch):
    """Bei der Rueckfuehrung wiegt ein fremder Tresor am schwersten.

    Anders als beim Pseudonymisieren (falsche Pseudonyme) wuerden hier die
    KLARNAMEN eines fremden Mandanten in dieses Dokument eingesetzt — ein
    Datenabfluss, kein Schoenheitsfehler. Analog zu
    ``test_fremder_vault_wird_abgewiesen`` fuer den ``anonymize``-Zweig.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus
    from backend.vault.models import Tenant, Vault

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    with session_factory() as s:
        fremder_mandant = Tenant(name="Fremdmandant")
        s.add(fremder_mandant)
        s.commit()
        s.refresh(fremder_mandant)

        fremder_vault = Vault(tenant_id=fremder_mandant.id, label="Fremdtresor")
        s.add(fremder_vault)
        s.commit()
        s.refresh(fremder_vault)

        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="reverse",
            dateiname="notiz.txt", daten=b"Text",
            parameter={"vault_id": str(fremder_vault.id)},
        )
        s.commit()

        with pytest.raises(ValueError, match="Zuordnungstabelle nicht gefunden"):
            fuehre_aus(job, b"Text", lambda a, b: None)


def test_reverse_ohne_vault_id_scheitert_verstaendlich(
    session_factory, mandant, monkeypatch
):
    """Ohne vault_id gibt es nichts zurueckzufuehren.

    Das muss verstaendlich scheitern statt mit einem rohen KeyError auf
    ``parameter["vault_id"]``.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="reverse",
            dateiname="notiz.txt", daten=b"Text", parameter={},
        )
        s.commit()

        with pytest.raises(ValueError, match="vault_id"):
            fuehre_aus(job, b"Text", lambda a, b: None)


# ── Nutzungserfassung (Nachbesserung 2026-08-26) ────────────────────────────


def test_der_auftragspfad_schreibt_einen_nutzungseintrag(
    session_factory, mandant, monkeypatch
):
    """Ohne ihn fehlen ausgerechnet die grossen Laeufe in jeder Auswertung.

    ``GET /api/v1/api-keys/usage`` zaehlt ``usage_events``. Der synchrone
    Pfad schreibt dort seit jeher, der Auftragspfad nicht — die Auswertung
    sah damit vollstaendig aus, war aber systematisch zu klein, und der
    Fehler waechst mit der Dateigroesse. Ein stiller Messfehler ist
    schlimmer als eine Luecke, die man sieht.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus
    from backend.vault.models import UsageEvent

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    text = "Die Auskunft erteilte Frau Nadia Wenger am Montag.".encode("utf-8")
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="apikey:4711", mode="anonymize",
            dateiname="notiz.txt", daten=text,
            parameter={"modus": "anonymous", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()
        fuehre_aus(job, text, lambda a, b: None)

    with session_factory() as s:
        eintraege = s.query(UsageEvent).all()
        assert len(eintraege) == 1
        eintrag = eintraege[0]
        # DERSELBE Aktionsname wie synchron. Eigene Namen wuerden dieselbe
        # Taetigkeit in der Auswertung auf zwei Zeilen aufteilen, die
        # niemand wieder zusammenrechnet.
        assert eintrag.action == "anonymize_file"
        # Der Aktor kommt aus der Auftragszeile, nicht aus einem Request —
        # der laeuft laengst nicht mehr. Bei einer Maschine mit Praefix.
        assert eintrag.username == "apikey:4711"
        assert eintrag.tenant_id == mandant.id
        assert eintrag.char_count > 0
        assert eintrag.entity_counts, "die Fundzahlen je Kategorie gehoeren dazu"


def test_der_nutzungseintrag_traegt_keinen_dateinamen(
    session_factory, mandant, monkeypatch
):
    """Nur Metadaten und Zahlen — nie Inhalte, nie Dateinamen (Fachkonzept §9).

    ``usage_events`` ist die eine Tabelle des Werkzeugs, die ausdruecklich
    unverschluesselt und auswertbar sein soll. Ein Dateiname darin waere
    genau die Art Personendatum, die durch die Hintertuer hereinkommt:
    „Einvernahme_Muster_Hans.docx".
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus
    from backend.vault.models import UsageEvent

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    text = b"Ein harmloser Satz ohne Namen."
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="tester", mode="anonymize",
            dateiname="Einvernahme_Muster_Hans.txt", daten=text,
            parameter={"modus": "anonymous", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()
        fuehre_aus(job, text, lambda a, b: None)

    with session_factory() as s:
        eintrag = s.query(UsageEvent).one()
        spalten = " ".join(
            str(getattr(eintrag, name.key)) for name in eintrag.__table__.columns
        )
        assert "Muster" not in spalten
        assert "Einvernahme" not in spalten


# ── Einstellungen des Laufs im Ergebnis (Nachbesserung 2026-08-26) ──────────


def test_das_auftragsergebnis_traegt_die_einstellungen_des_laufs(
    session_factory, mandant, monkeypatch
):
    """Ohne sie faellt die Oberflaeche nach einer Wiederaufnahme auf Vorgaben zurueck.

    Wer die Seite neu laedt, hat seine Bedienelemente von damals nicht mehr —
    er hat nur die Auftragskennung. Fehlen Kategorienwahl und KI-Haekchen im
    Umschlag, schickt "Anwenden" (Fehlalarme abwaehlen) irgendetwas: eine
    leere Kategorienliste, die der Server als Bedienfehler abweist, und den
    Vorgabemodus ``umkehrbar`` — der fuer einen ``anonymous``-Lauf einen
    Tresor anlegte, den niemand bestellt hat. Klarnamen in einer
    Zuordnungstabelle, ohne dass es jemand angeordnet haette.

    Der Umschlag ist damit eine Obermenge der synchronen Antwort. Das ist
    Absicht und in API.md und ``routers/jobs.py`` so begruendet.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    text = "Die Auskunft erteilte Frau Nadia Wenger am Montag.".encode("utf-8")
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=text,
            parameter={"modus": "anonymous", "entities": ["PERSON"],
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()

        ergebnis = fuehre_aus(job, text, lambda a, b: None)

    zusatz = ergebnis.zusatz
    assert zusatz["mode"] == "anonymous"
    assert zusatz["requested_entities"] == ["PERSON"]
    assert zusatz["ki_pruefung"] is False

    # Der Namenstest: ``entities`` bleibt die FUNDLISTE. Waeren die gesuchten
    # Kategorien unter demselben Schluessel gelandet, haette der eine den
    # anderen still ueberschrieben — die Wiederaufnahme zeigte dann statt der
    # Funde die Wortliste "PERSON", und niemand koennte einen Fehlalarm mehr
    # abwaehlen.
    assert zusatz["entities"], "die Fundliste gehoert weiterhin unter entities"
    assert all(isinstance(f, dict) for f in zusatz["entities"])
    assert zusatz["entities"] != zusatz["requested_entities"]


def test_ohne_ausdrueckliche_kategorienwahl_steht_dort_none(
    session_factory, mandant, monkeypatch
):
    """``None`` heisst "keine Wahl getroffen", nicht "keine Kategorie".

    Ueber ``/api/ext/v1`` darf ``entities`` fehlen; dann gilt die Vorgabe des
    Servers. Stuende dafuer eine leere Liste im Umschlag, laese der Abholer
    daraus "nichts gesucht" — und die Oberflaeche zeigte bei der
    Wiederaufnahme den Bedienfehler "Waehlen Sie mindestens eine Kategorie",
    obwohl der Lauf mit den Vorgaben sauber durchgelaufen ist.
    """
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    monkeypatch.setattr(
        "backend.jobs.ausfuehrung.get_sessionmaker", lambda: session_factory
    )

    text = b"Ein harmloser Satz."
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=text,
            parameter={"modus": "anonymous", "entities": None,
                       "ki_pruefung": True, "selected_entities": None},
        )
        s.commit()

        ergebnis = fuehre_aus(job, text, lambda a, b: None)

    assert ergebnis.zusatz["requested_entities"] is None
    # Angefordert ist nicht gelaufen: ohne Netzverkehr (kein Schluessel im
    # Testbetrieb) bleibt ``ki_geprueft`` falsch, das Haekchen aber wahr.
    assert ergebnis.zusatz["ki_pruefung"] is True
