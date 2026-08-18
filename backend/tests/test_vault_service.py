"""Tests des Vault-Dienstes: Stabilitaet, Nummerierung, Rueckfuehrung.

Die Tests laufen gegen **SQLite in-memory**. Begruendung und die drei dafuer
noetigen Portabilitaets-Entscheidungen stehen im Kopf von
``backend/vault/models.py``. Der Kern: ein Testlauf soll keinen laufenden
Datenbankdienst voraussetzen, sonst scheitert das Golden-Set-Gate bei jedem
Entwickler zuerst an der Infrastruktur.

Alle Testdaten sind synthetisch — echte Personendaten gehoeren nach Hausregel
nicht ins Repository.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db import create_db_engine, init_db
from backend.vault import crypto, service
from backend.vault.models import MappingEntry, Tenant, Vault
from backend.vault.pseudonyms import parse_pseudonym

TEST_MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


@pytest.fixture(autouse=True)
def master_key_gesetzt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setzt den Master-Key fuer jeden Test."""
    monkeypatch.setenv("VAULT_MASTER_KEY", TEST_MASTER_KEY)


@pytest.fixture
def session() -> Iterator[Session]:
    """Frische SQLite-In-Memory-Datenbank je Test.

    ``create_db_engine`` erzwingt fuer SQLite den ``StaticPool`` — ohne ihn
    saehe jede Verbindung eine eigene, leere Datenbank.
    """
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as offene_session:
        yield offene_session
    engine.dispose()


@pytest.fixture
def vault(session: Session) -> Vault:
    """Ein Mandant mit einem leeren Vault."""
    tenant = Tenant(name="Testmandant")
    session.add(tenant)
    session.flush()
    neuer_vault = Vault(tenant_id=tenant.id, label="Testfall 2026-001")
    session.add(neuer_vault)
    session.flush()
    return neuer_vault


def eintragszahl(session: Session, vault: Vault) -> int:
    """Anzahl der Zuordnungen im Vault."""
    return session.execute(
        select(func.count()).select_from(MappingEntry).where(
            MappingEntry.vault_id == vault.id
        )
    ).scalar_one()


# ── Testfall 4: Stabilitaet ────────────────────────────────────────────────


def test_derselbe_name_bekommt_immer_dasselbe_pseudonym(
    session: Session, vault: Vault
) -> None:
    """Zweimal derselbe Name -> dasselbe Pseudonym, nur ein Datenbankeintrag.

    Die zentrale Zusage des Vaults. Ohne sie traege dieselbe Person in Akte 1
    ein anderes Symbol als in Akte 2, und der Zusammenhang, um dessentwillen
    ueberhaupt pseudonymisiert statt geschwaerzt wird, waere zerstoert.
    """
    erstes = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    zweites = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")

    assert erstes == zweites == "[[PERSON_1]]"
    assert eintragszahl(session, vault) == 1


def test_stabilitaet_ueberdauert_einen_commit(session: Session, vault: Vault) -> None:
    """Auch nach Commit und Neuladen wird der bestehende Eintrag gefunden.

    Prueft den realistischen Fall: zwei Dokumente desselben Falls, in getrennten
    Anfragen verarbeitet.
    """
    erstes = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    session.commit()
    session.expire_all()

    zweites = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")

    assert erstes == zweites
    assert eintragszahl(session, vault) == 1


def test_verschiedene_vaults_vergeben_unabhaengige_pseudonyme(
    session: Session, vault: Vault
) -> None:
    """Zwei Vaults desselben Mandanten teilen sich keine Nummerierung.

    Sonst liessen sich zwei Faelle ueber ein gemeinsames ``[[PERSON_3]]``
    miteinander verknuepfen.
    """
    zweiter_vault = Vault(tenant_id=vault.tenant_id, label="Testfall 2026-002")
    session.add(zweiter_vault)
    session.flush()

    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    service.get_or_create_pseudonym(session, vault, "PERSON", "Bruno Muster")
    im_zweiten = service.get_or_create_pseudonym(
        session, zweiter_vault, "PERSON", "Bruno Muster"
    )

    # Im ersten Vault ist Bruno Muster die Nummer 2, im zweiten die Nummer 1.
    assert im_zweiten == "[[PERSON_1]]"


def test_gleicher_text_als_zwei_entitaetstypen_bleibt_getrennt(
    session: Session, vault: Vault
) -> None:
    """Eine Einzelfirma, die wie ihr Inhaber heisst, ergibt zwei Zuordnungen."""
    als_person = service.get_or_create_pseudonym(
        session, vault, "PERSON", "Bruno Muster"
    )
    als_firma = service.get_or_create_pseudonym(
        session, vault, "ORGANIZATION", "Bruno Muster"
    )

    assert als_person == "[[PERSON_1]]"
    assert als_firma == "[[ORGANIZATION_1]]"
    assert eintragszahl(session, vault) == 2


# ── Testfall 5: Nummerierung ───────────────────────────────────────────────


def test_verschiedene_namen_werden_aufsteigend_nummeriert(
    session: Session, vault: Vault
) -> None:
    """Je Entitaetstyp laeuft ein eigener Zaehler, luekenlos aufsteigend."""
    namen = ["Andrea Beispiel", "Bruno Muster", "Carla Probe"]
    pseudonyme = [
        service.get_or_create_pseudonym(session, vault, "PERSON", name) for name in namen
    ]

    assert pseudonyme == ["[[PERSON_1]]", "[[PERSON_2]]", "[[PERSON_3]]"]
    assert len(set(pseudonyme)) == 3


def test_zaehler_laufen_je_entitaetstyp_getrennt(
    session: Session, vault: Vault
) -> None:
    """PERSON und LOCATION zaehlen unabhaengig voneinander ab 1."""
    assert (
        service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
        == "[[PERSON_1]]"
    )
    assert (
        service.get_or_create_pseudonym(session, vault, "LOCATION", "Musterstadt")
        == "[[LOCATION_1]]"
    )
    assert (
        service.get_or_create_pseudonym(session, vault, "PERSON", "Bruno Muster")
        == "[[PERSON_2]]"
    )
    assert (
        service.get_or_create_pseudonym(session, vault, "LOCATION", "Beispieldorf")
        == "[[LOCATION_2]]"
    )


def test_entitaetstyp_wird_normiert(session: Session, vault: Vault) -> None:
    """Kleingeschriebener Typ ergibt dasselbe Pseudonym wie der grosse."""
    erstes = service.get_or_create_pseudonym(session, vault, "person", "Andrea Beispiel")
    zweites = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")

    assert erstes == zweites == "[[PERSON_1]]"
    assert eintragszahl(session, vault) == 1


# ── Testfall 6: Rueckfuehrungs-Determinismus ───────────────────────────────


def test_rueckfuehrung_ist_byte_identisch(session: Session, vault: Vault) -> None:
    """Der zentrale Test: Original -> ersetzen -> reverse -> byte-identisch.

    Wenn diese Zusage bricht, ist das Werkzeug fuer seinen Zweck unbrauchbar:
    ein zurueckgefuehrtes Dokument, das vom Original abweicht, ist als
    Rechtsdokument wertlos, und die Abweichung faellt niemandem auf.
    """
    original = (
        "Andrea Beispiel wohnt in Musterstadt. Bruno Muster hat Andrea Beispiel "
        "am 3. Maerz in Musterstadt getroffen; Carla Probe war ebenfalls dort.\n"
        "Rueckfrage an Bruno Muster: siehe Beilage 4."
    )

    ersetzungen = {
        "PERSON": ["Andrea Beispiel", "Bruno Muster", "Carla Probe"],
        "LOCATION": ["Musterstadt"],
    }

    pseudonymisiert = original
    for entity_type, werte in ersetzungen.items():
        for wert in werte:
            pseudonym = service.get_or_create_pseudonym(
                session, vault, entity_type, wert
            )
            pseudonymisiert = pseudonymisiert.replace(wert, pseudonym)

    # Zwischenpruefung: im pseudonymisierten Text darf kein Klarname mehr stehen.
    for werte in ersetzungen.values():
        for wert in werte:
            assert wert not in pseudonymisiert

    zurueckgefuehrt = service.reverse(session, vault, pseudonymisiert)

    assert zurueckgefuehrt == original


def test_rueckfuehrung_erhaelt_die_erste_schreibweise(
    session: Session, vault: Vault
) -> None:
    """Gespeichert wird das Original, nicht der normalisierte Wert.

    Eine kleingeschriebene Rueckfuehrung waere ein stiller Datenverlust.
    """
    pseudonym = service.get_or_create_pseudonym(
        session, vault, "PERSON", "Andrea Beispiel"
    )

    assert service.reverse(session, vault, f"Zeuge: {pseudonym}.") == (
        "Zeuge: Andrea Beispiel."
    )


def test_rueckfuehrung_laesst_unbekannten_text_unberuehrt(
    session: Session, vault: Vault
) -> None:
    """Ein Text ohne Pseudonyme des Vaults kommt unveraendert zurueck."""
    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    text = "Hier steht [[PERSON_99]] und sonst nichts Bekanntes."

    assert service.reverse(session, vault, text) == text


def test_rueckfuehrung_eines_leeren_vaults(session: Session, vault: Vault) -> None:
    """Ohne Eintraege ist die Rueckfuehrung die Identitaet."""
    assert service.reverse(session, vault, "Unveraendert.") == "Unveraendert."


def test_ein_klartext_der_wie_ein_pseudonym_aussieht_wird_nicht_erneut_ersetzt(
    session: Session, vault: Vault
) -> None:
    """Ein-Pass-Ersetzung: eingesetzter Klartext wird nicht nochmals ersetzt.

    Konstruierter Grenzfall, der bei einer Kette von ``str.replace`` zuschlagen
    wuerde: das Original von ``[[PERSON_1]]`` enthaelt selbst die Zeichenfolge
    ``[[PERSON_2]]``.
    """
    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea [[PERSON_2]]")
    service.get_or_create_pseudonym(session, vault, "PERSON", "Bruno Muster")

    ergebnis = service.reverse(session, vault, "[[PERSON_1]]")

    assert ergebnis == "Andrea [[PERSON_2]]"


# ── Testfall 7: Praefix-Kollision ──────────────────────────────────────────


def test_praefix_kollision_bei_zweistelligen_zaehlern(
    session: Session, vault: Vault
) -> None:
    """``[[PERSON_11]]`` darf nie als ``[[PERSON_1]]`` plus ``1]]`` gelten.

    Deshalb ersetzt ``reverse`` die laengsten Pseudonyme zuerst und in einem
    einzigen Durchlauf.
    """
    namen = [f"Person Nummer {nummer:02d}" for nummer in range(1, 13)]
    pseudonyme = [
        service.get_or_create_pseudonym(session, vault, "PERSON", name) for name in namen
    ]

    assert pseudonyme[0] == "[[PERSON_1]]"
    assert pseudonyme[10] == "[[PERSON_11]]"
    assert pseudonyme[11] == "[[PERSON_12]]"

    # Reihenfolge bewusst so, dass ein naiver Praefix-Ersatz auffliegt.
    text = "A=[[PERSON_1]] B=[[PERSON_11]] C=[[PERSON_12]] D=[[PERSON_2]]"
    erwartet = (
        "A=Person Nummer 01 B=Person Nummer 11 C=Person Nummer 12 D=Person Nummer 02"
    )

    assert service.reverse(session, vault, text) == erwartet


def test_rueckfuehrung_mit_zwoelf_personen_ist_byte_identisch(
    session: Session, vault: Vault
) -> None:
    """Vollstaendiger Roundtrip mit zweistelligen Zaehlern.

    Die Namen werden absteigend nach Laenge ersetzt, damit schon die
    Pseudonymisierung keine Teiltreffer erzeugt.
    """
    namen = [f"Person Nummer {nummer:02d}" for nummer in range(1, 13)]
    original = " ".join(f"{name} sagte aus." for name in namen)

    pseudonymisiert = original
    for name in sorted(namen, key=len, reverse=True):
        pseudonym = service.get_or_create_pseudonym(session, vault, "PERSON", name)
        pseudonymisiert = pseudonymisiert.replace(name, pseudonym)

    assert "[[PERSON_11]]" in pseudonymisiert
    assert service.reverse(session, vault, pseudonymisiert) == original


# ── Testfall 8: Normalisierung ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "schreibweise",
    [
        "Andreas Meier",
        "andreas meier",
        "ANDREAS MEIER",
        "Andreas  Meier",
        "  Andreas Meier  ",
        "Andreas\tMeier",
        "Andreas Meier",
    ],
)
def test_schreibweisen_landen_im_selben_cluster(
    session: Session, vault: Vault, schreibweise: str
) -> None:
    """Gross-/Kleinschreibung und Whitespace duerfen kein neues Cluster oeffnen.

    Der Anlass ist praktisch: Ueberschriften stehen in Versalien, Tabellenzellen
    brechen mit doppeltem Leerzeichen um, DOCX-Exporte liefern geschuetzte
    Leerzeichen. Ohne Normalisierung entstuenden fuer dieselbe Person mehrere
    Pseudonyme.
    """
    erstes = service.get_or_create_pseudonym(session, vault, "PERSON", "Andreas Meier")
    zweites = service.get_or_create_pseudonym(session, vault, "PERSON", schreibweise)

    assert erstes == zweites == "[[PERSON_1]]"
    assert eintragszahl(session, vault) == 1


def test_normalisierung_legt_verschiedene_personen_nicht_zusammen(
    session: Session, vault: Vault
) -> None:
    """Was verschieden ist, bleibt verschieden — auch nach Normalisierung."""
    erstes = service.get_or_create_pseudonym(session, vault, "PERSON", "Andreas Meier")
    zweites = service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Meier")
    drittes = service.get_or_create_pseudonym(session, vault, "PERSON", "Meier Andreas")

    assert len({erstes, zweites, drittes}) == 3


def test_normalize_entity_arbeitet_wie_dokumentiert() -> None:
    """Die Normalisierung selbst, ohne Datenbank."""
    assert service.normalize_entity("  Andreas   Meier \n") == "andreas meier"
    assert service.normalize_entity("ANDREAS MEIER") == "andreas meier"
    assert service.normalize_entity("Andreas Meier") == "andreas meier"


# ── Auflistung, Loeschung, Speicherform ────────────────────────────────────


def test_der_klarname_steht_nirgends_im_klartext_in_der_zeile(
    session: Session, vault: Vault
) -> None:
    """Ciphertext und HMAC verraten den Namen nicht — der Kern des Entwurfs."""
    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    eintrag = session.execute(select(MappingEntry)).scalar_one()

    klarname = b"Andrea Beispiel"
    assert klarname not in bytes(eintrag.original_ciphertext)
    assert klarname.lower() not in bytes(eintrag.original_ciphertext)
    assert klarname not in bytes(eintrag.cluster_key_hmac)
    # Das Pseudonym dagegen ist bewusst Klartext — es ist das Ersatzsymbol.
    assert eintrag.pseudonym == "[[PERSON_1]]"
    # Und der Klarname ist mit dem Master-Key wiederherstellbar.
    assert crypto.unseal(
        str(eintrag.tenant_id), bytes(eintrag.original_ciphertext)
    ) == "Andrea Beispiel"


def test_list_entries_liefert_die_zuordnungen(session: Session, vault: Vault) -> None:
    """Auflistung gesamt und nach Entitaetstyp gefiltert."""
    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    service.get_or_create_pseudonym(session, vault, "LOCATION", "Musterstadt")

    alle = service.list_entries(session, vault)
    nur_personen = service.list_entries(session, vault, entity_type="PERSON")

    assert len(alle) == 2
    assert [eintrag.pseudonym for eintrag in nur_personen] == ["[[PERSON_1]]"]


def test_delete_vault_loescht_die_eintraege_mit(session: Session, vault: Vault) -> None:
    """Der Vault verschwindet samt Zuordnungen; kein verwaister Ciphertext."""
    service.get_or_create_pseudonym(session, vault, "PERSON", "Andrea Beispiel")
    service.get_or_create_pseudonym(session, vault, "PERSON", "Bruno Muster")
    vault_id = vault.id

    geloescht = service.delete_vault(session, vault)
    session.commit()

    assert geloescht == 2
    assert session.get(Vault, vault_id) is None
    assert (
        session.execute(select(func.count()).select_from(MappingEntry)).scalar_one() == 0
    )


def test_pseudonyme_sind_parsebar(session: Session, vault: Vault) -> None:
    """Erzeuger und Parser des Formats passen zusammen."""
    pseudonym = service.get_or_create_pseudonym(
        session, vault, "PERSON", "Andrea Beispiel"
    )

    assert parse_pseudonym(pseudonym) == ("PERSON", 1)
    assert parse_pseudonym("Person B") is None
