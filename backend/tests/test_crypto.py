"""Tests der Vault-Kryptografie.

Alle Testdaten sind synthetisch. Echte Personendaten gehoeren nach Hausregel
nicht ins Repository — auch nicht in Fixtures.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from cryptography.exceptions import InvalidTag

from backend.vault import crypto

# Fester Testschluessel. Er ist oeffentlich und darf es sein: er schuetzt nichts.
TEST_MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def master_key_gesetzt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setzt den Master-Key fuer jeden Test und raeumt ihn danach wieder weg."""
    monkeypatch.setenv("VAULT_MASTER_KEY", TEST_MASTER_KEY)


# ── Master-Key ─────────────────────────────────────────────────────────────


def test_fehlender_master_key_meldet_sich_klar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne Master-Key gibt es eine deutsche Fehlermeldung, keinen AttributeError."""
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)

    with pytest.raises(crypto.VaultKeyError) as fehler:
        crypto.ensure_master_key()

    meldung = str(fehler.value)
    assert "VAULT_MASTER_KEY" in meldung
    assert "openssl rand -hex 32" in meldung


def test_master_key_mit_falscher_laenge_wird_abgelehnt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ein zu kurzer Schluessel faellt beim Start auf, nicht beim ersten Schreiben."""
    monkeypatch.setenv("VAULT_MASTER_KEY", "abcd")

    with pytest.raises(crypto.VaultKeyError, match="Laenge"):
        crypto.ensure_master_key()


def test_master_key_ohne_hex_wird_abgelehnt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Richtige Laenge, aber kein Hex — ebenfalls ein Startfehler."""
    monkeypatch.setenv("VAULT_MASTER_KEY", "z" * 64)

    with pytest.raises(crypto.VaultKeyError, match="Hex"):
        crypto.ensure_master_key()


# ── Testfall 1: Roundtrip und Manipulationserkennung ───────────────────────


@pytest.mark.parametrize(
    "klartext",
    [
        "Andrea Beispiel",
        "Muster AG, Bahnhofstrasse 1, 3000 Musterstadt",
        "Sonderzeichen: ae oe ue, Akzente: Cecile Rene, Emoji: \U0001f512",
        "",
        "x" * 5000,
    ],
)
def test_seal_open_roundtrip(klartext: str) -> None:
    """Was versiegelt wurde, kommt unveraendert wieder heraus."""
    blob = crypto.seal(TENANT_A, klartext)

    assert crypto.unseal(TENANT_A, blob) == klartext


def test_blob_hat_das_vereinbarte_drahtformat() -> None:
    """Nonce (12) + Ciphertext + Tag (16) — die Laenge muss exakt aufgehen."""
    klartext = "Andrea Beispiel"
    blob = crypto.seal(TENANT_A, klartext)

    erwartet = crypto.NONCE_BYTES + len(klartext.encode("utf-8")) + crypto.TAG_BYTES
    assert len(blob) == erwartet


def test_zweimal_versiegeln_ergibt_verschiedene_blobs() -> None:
    """Zufaellige Nonce: gleicher Klartext darf nie denselben Blob ergeben.

    Waere er deterministisch, koennte man aus der Datenbank ablesen, welche
    Eintraege dasselbe Original haben — genau die Information, die der
    Cluster-HMAC kontrolliert freigeben soll.
    """
    erster = crypto.seal(TENANT_A, "Andrea Beispiel")
    zweiter = crypto.seal(TENANT_A, "Andrea Beispiel")

    assert erster != zweiter
    assert crypto.unseal(TENANT_A, erster) == crypto.unseal(TENANT_A, zweiter)


@pytest.mark.parametrize(
    ("position", "bereich"),
    [
        (0, "Nonce"),
        (crypto.NONCE_BYTES, "Ciphertext"),
        (-1, "Tag"),
    ],
)
def test_manipuliertes_byte_wirft_statt_falschen_klartext(
    position: int, bereich: str
) -> None:
    """Ein gekipptes Bit muss knallen — nicht stillschweigend Muell liefern.

    Das ist die eigentliche Zusage von GCM gegenueber einem reinen Stromchiffre:
    ein veraenderter Blob ist erkennbar veraendert. Ein falscher Klartext, der
    als echt durchgeht, waere im Vault der schlimmste denkbare Fehler — er
    ergaebe eine Rueckfuehrung auf den falschen Namen.
    """
    blob = bytearray(crypto.seal(TENANT_A, "Andrea Beispiel"))
    blob[position] ^= 0x01

    with pytest.raises(InvalidTag):
        crypto.unseal(TENANT_A, bytes(blob))
    assert bereich  # dokumentiert nur, welcher Abschnitt getroffen wurde


def test_abgeschnittener_blob_wird_als_solcher_gemeldet() -> None:
    """Zu kurz fuer Nonce + Tag: eigener, sprechender Fehler statt IndexError."""
    with pytest.raises(crypto.VaultSealError, match="zu kurz"):
        crypto.unseal(TENANT_A, b"kaputt")


# ── Testfall 2: Mandantentrennung ──────────────────────────────────────────


def test_blob_von_mandant_a_laesst_sich_nicht_mit_b_oeffnen() -> None:
    """Ein Blob ist an seinen Mandanten gebunden — durch Schluessel und AAD.

    Deckt den realistischen Fehlerfall ab, dass eine Zeile per Migration,
    Copy-Paste-Skript oder Bug im falschen Mandanten landet: sie ist dort nicht
    lesbar, statt dort einen fremden Klarnamen auszugeben.
    """
    blob = crypto.seal(TENANT_A, "Andrea Beispiel")

    with pytest.raises(InvalidTag):
        crypto.unseal(TENANT_B, blob)


def test_abgeleitete_schluessel_unterscheiden_sich_je_mandant_und_zweck() -> None:
    """HKDF trennt nach Mandant und nach Verwendungszweck."""
    a_seal = crypto.derive_tenant_key(TENANT_A, crypto.PURPOSE_SEAL)
    b_seal = crypto.derive_tenant_key(TENANT_B, crypto.PURPOSE_SEAL)
    a_cluster = crypto.derive_tenant_key(TENANT_A, crypto.PURPOSE_CLUSTER)

    assert len({a_seal, b_seal, a_cluster}) == 3
    assert len(a_seal) == crypto.KEY_BYTES
    # Der Master-Key selbst darf nie als Arbeitsschluessel auftauchen.
    assert a_seal != bytes.fromhex(TEST_MASTER_KEY)


def test_anderer_master_key_oeffnet_denselben_blob_nicht(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belegt, dass der Master-Key wirklich die Wurzel der Ableitung ist."""
    blob = crypto.seal(TENANT_A, "Andrea Beispiel")
    monkeypatch.setenv("VAULT_MASTER_KEY", "ff" * 32)

    with pytest.raises(InvalidTag):
        crypto.unseal(TENANT_A, blob)


# ── Testfall 3: Cluster-HMAC ───────────────────────────────────────────────


def test_cluster_hmac_ist_deterministisch() -> None:
    """Gleicher Eingabewert, gleicher Mandant -> gleicher Digest.

    Darauf beruht der gesamte Gleichheits-Lookup: ohne Determinismus faende
    ``get_or_create_pseudonym`` einen bestehenden Eintrag nie wieder.
    """
    erster = crypto.cluster_key_hmac(TENANT_A, "andrea beispiel")
    zweiter = crypto.cluster_key_hmac(TENANT_A, "andrea beispiel")

    assert erster == zweiter
    assert len(erster) == 32


def test_cluster_hmac_unterscheidet_mandanten() -> None:
    """Derselbe Name ergibt bei verschiedenen Mandanten verschiedene Digests.

    Sonst liesse sich ueber einen Join auf ``cluster_key_hmac`` feststellen, ob
    dieselbe Person bei zwei Mandanten vorkommt.
    """
    assert crypto.cluster_key_hmac(TENANT_A, "andrea beispiel") != crypto.cluster_key_hmac(
        TENANT_B, "andrea beispiel"
    )


def test_cluster_hmac_unterscheidet_verschiedene_werte() -> None:
    """Verschiedene Eingabewerte ergeben verschiedene Digests."""
    digests = {
        crypto.cluster_key_hmac(TENANT_A, wert)
        for wert in ("andrea beispiel", "andrea beispiei", "beispiel andrea", "")
    }

    assert len(digests) == 4


def test_klartext_ist_aus_dem_cluster_hmac_nicht_ablesbar() -> None:
    """Der Digest gibt den Klarnamen weder direkt noch per Woerterbuch preis.

    Geprueft wird beides:

    * Der Klartext steht nicht als Byte-Folge im Digest (der naive Fehler, den
      der urspruengliche Klartext-``cluster_key`` gemacht haette).
    * Der Digest ist **nicht** ein ungeschluesselter Hash. Waere er es, koennte
      ein Angreifer mit einem Namensverzeichnis alle Eintraege durchprobieren —
      der Suchraum von Personennamen ist klein genug dafuer. Erst der geheime,
      abgeleitete Schluessel macht das unmoeglich.
    """
    klartext = "Andrea Beispiel"
    digest = crypto.cluster_key_hmac(TENANT_A, klartext)

    assert klartext.encode("utf-8") not in digest
    assert klartext.casefold().encode("utf-8") not in digest
    assert digest != hashlib.sha256(klartext.encode("utf-8")).digest()
    assert digest != hashlib.sha256(klartext.casefold().encode("utf-8")).digest()
    # Und ohne Kenntnis des Master-Keys ist der Digest nicht nachzubauen.
    fremder_schluessel = os.urandom(32)
    assert digest != hashlib.sha256(fremder_schluessel + klartext.encode()).digest()


# ── Testfall 4: Rohbytes versiegeln ───────────────────────────────────────────


def test_bytes_ueberstehen_die_versiegelung() -> None:
    """Ein DOCX ist keine UTF-8-Zeichenkette — seal() traegt es nicht."""
    rohdaten = bytes(range(256)) * 4  # alle Bytewerte, auch ungueltiges UTF-8
    blob = crypto.seal_bytes(TENANT_A, rohdaten)

    assert blob != rohdaten, "es muss verschluesselt sein"
    assert crypto.open_bytes(TENANT_A, blob) == rohdaten


def test_fremder_mandant_kann_bytes_nicht_oeffnen() -> None:
    """Die Mandanten-Id geht als AAD mit ein — dieselbe Sperre wie bei seal()."""
    blob = crypto.seal_bytes(TENANT_A, b"vertraulich")
    with pytest.raises(InvalidTag):
        crypto.open_bytes(TENANT_B, blob)
