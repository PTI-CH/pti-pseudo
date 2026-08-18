"""Kryptografie des Vaults: Schluesselableitung, AES-256-GCM, Cluster-HMAC.

Bedrohungsmodell in einem Absatz: Angenommen wird ein Angreifer mit Lesezugriff
auf die Datenbank (Dump, Backup-Band, kompromittierter DB-Benutzer), aber ohne
Zugriff auf die Prozess-Umgebung der Anwendung. Der Master-Key liegt
ausschliesslich in der Env (Jelastic-Dashboard) und nie in der Datenbank. Ein
DB-Dump allein gibt damit weder Klarnamen noch die Moeglichkeit, sie zu
rekonstruieren.

Warum der ``cluster_key`` ein HMAC ist und kein Klartext
--------------------------------------------------------
Das Fachkonzept (Paragraph 3) sah vor, neben dem verschluesselten Original
zusaetzlich einen ``cluster_key`` im **Klartext** zu speichern, damit man
gleiche Entitaeten wiederfindet ("Andreas Meier" soll im selben Vault immer
dasselbe Pseudonym bekommen).

Das haette die Verschluesselung wertlos gemacht: der Klarname staende in der
Nachbarspalte derselben Zeile. Wer ``original_ciphertext`` nicht lesen kann,
haette ihn dann auch gar nicht lesen muessen.

Der HMAC loest dieselbe Aufgabe ohne diese Preisgabe:

* Gleichheits-Lookup bleibt moeglich — gleicher Eingabewert ergibt denselben
  Digest, also findet ``SELECT ... WHERE cluster_key_hmac = ?`` den Eintrag.
* Der Klartext ist daraus nicht ablesbar. SHA-256 ist nicht umkehrbar, und der
  bei einem Namensraum sonst naheliegende Woerterbuch-Angriff scheitert am
  geheimen, pro Mandant abgeleiteten HMAC-Schluessel: ohne ihn lassen sich
  Kandidaten nicht einmal durchprobieren.

Was der HMAC bewusst **nicht** leistet: Aehnlichkeits- oder Praefix-Suche
("Meier" findet nicht "Meier-Muster"). Das Clustering laeuft deshalb ueber die
Normalisierung in ``service.normalize_entity`` — vor dem HMAC, nicht danach.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# -- Konstanten des Drahtformats --------------------------------------------
# 12 Byte ist die von NIST SP 800-38D empfohlene Nonce-Laenge fuer GCM: nur bei
# genau 12 Byte wird die Nonce direkt als Zaehlerblock verwendet, laengere
# werden zusaetzlich gehasht (langsamer, ohne Sicherheitsgewinn).
NONCE_BYTES = 12
# AESGCM aus ``cryptography`` haengt den 16-Byte-Tag selbst an den Ciphertext an;
# die Konstante dient hier nur der Laengenpruefung beim Oeffnen.
TAG_BYTES = 16
# AES-256 — passend zu den 32 Byte, die HKDF liefert.
KEY_BYTES = 32
# Der Master-Key kommt als Hex, damit er sich in einer Env-Variablen und im
# Passwortmanager gefahrlos transportieren laesst.
MASTER_KEY_HEX_LEN = KEY_BYTES * 2

# Domain-Separation der abgeleiteten Schluessel. Der Versionsanteil erlaubt
# spaeter einen Schluesselwechsel, ohne dass alte Blobs mehrdeutig werden.
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │ DIESE DREI ZEICHENKETTEN NIEMALS AENDERN.                               │
# │                                                                         │
# │ Sie gehen als HKDF-Info bzw. -Salt in die Schluesselableitung ein. Ein  │
# │ geaendertes Byte ergibt einen anderen Schluessel — und damit sind ALLE  │
# │ bestehenden Tresore unlesbar, obwohl der VAULT_MASTER_KEY unveraendert  │
# │ ist. Der Fehler faellt erst beim naechsten `documents:reverse` auf.     │
# │                                                                         │
# │ Sie sehen aus wie Namen, sind aber keine: dass hier der alte Repo-Name  │
# │ steht, ist ohne Bedeutung. Bei einer Umbenennung des Projekts, des      │
# │ Repositories oder der Umgebung bleiben sie, wie sie sind. Ein echter    │
# │ Wechsel laeuft ueber den Versionsanteil (/v2) PLUS ein Umschluesselungs-│
# │ skript, das jeden Eintrag mit dem alten Schluessel oeffnet und mit dem  │
# │ neuen wieder verschliesst.                                              │
# └─────────────────────────────────────────────────────────────────────────┘
PURPOSE_SEAL = b"pti-pseudonymisierer/vault/aes-gcm/v1"
PURPOSE_CLUSTER = b"pti-pseudonymisierer/vault/cluster-hmac/v1"
_SALT_NAMESPACE = b"pti-pseudonymisierer/vault/salt/v1"


class VaultKeyError(RuntimeError):
    """Der Master-Key fehlt oder hat ein unbrauchbares Format.

    Wird bewusst schon beim Start ausgeloest (siehe :func:`ensure_master_key`)
    und nicht erst beim ersten Schreibzugriff: eine Instanz ohne Master-Key kann
    keinen einzigen Vault-Vorgang ausfuehren, also soll sie gar nicht erst
    hochkommen.
    """


class VaultSealError(ValueError):
    """Ein Blob ist strukturell unbrauchbar (zu kurz, abgeschnitten)."""


def _setting(attribute: str, env_name: str) -> str | None:
    """Liest eine Einstellung aus ``backend.config``, mit Env als Rueckfallebene.

    ``backend.config`` ist die vorgesehene Quelle. Der Import liegt absichtlich
    in der Funktion und nicht am Modulkopf: dieses Modul soll importierbar
    bleiben (fuer Tests und Werkzeuge), auch wenn die Konfigurationsschicht noch
    nicht existiert oder beim Laden scheitert. Faellt sie aus, wird direkt die
    Umgebungsvariable gelesen — dieselbe Quelle, aus der sich die Settings
    ohnehin speisen.
    """
    settings = None
    try:
        from backend.config import get_settings

        settings = get_settings()
    except Exception:  # pragma: no cover — nur ohne/mit defekter Konfigschicht
        settings = None

    if settings is not None:
        value = getattr(settings, attribute, None)
        if value:
            return str(value)

    return os.environ.get(env_name) or None


def get_master_key() -> bytes:
    """Gibt den Master-Key als 32 Byte zurueck.

    Bewusst ohne Caching: der Wert wird pro Aufruf frisch aus den Settings
    gelesen. Ein Hex-Decode von 64 Zeichen ist billig, und ein Cache haette in
    Tests und beim Neuladen der Konfiguration nur Ueberraschungen erzeugt. Die
    teure Ableitung dahinter ist sehr wohl gecached (:func:`_derive`).
    """
    raw = _setting("vault_master_key", "VAULT_MASTER_KEY")
    if not raw:
        raise VaultKeyError(
            "VAULT_MASTER_KEY ist nicht gesetzt. Der Vault kann ohne Master-Key "
            "weder Zuordnungen speichern noch bestehende entschluesseln. "
            "Erzeugen mit: openssl rand -hex 32 — danach als Umgebungsvariable "
            "hinterlegen (in Produktion im Jelastic-Dashboard, gefolgt von einem "
            "Dashboard-Restart). ACHTUNG: Der Schluessel gehoert zusaetzlich in "
            "den Passwortmanager. Sein Verlust macht ALLE Vaults "
            "unwiederbringlich unlesbar."
        )

    raw = raw.strip()
    if len(raw) != MASTER_KEY_HEX_LEN:
        raise VaultKeyError(
            f"VAULT_MASTER_KEY hat die falsche Laenge: {len(raw)} Hex-Zeichen "
            f"statt {MASTER_KEY_HEX_LEN}. Erwartet werden {KEY_BYTES} Byte in "
            "Hex-Schreibweise: openssl rand -hex 32"
        )

    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise VaultKeyError(
            "VAULT_MASTER_KEY ist kein gueltiges Hex. Erwartet werden "
            f"{MASTER_KEY_HEX_LEN} Zeichen aus 0-9a-f: openssl rand -hex 32"
        ) from exc


def ensure_master_key() -> None:
    """Startup-Check: prueft den Master-Key, bevor der Server Anfragen annimmt.

    Aufzurufen im Lifespan-Hook der FastAPI-Anwendung. Ohne diesen Check faellt
    das Fehlen erst beim ersten Schreibvorgang auf — also mitten in einem
    Nutzer-Upload, mit einem 500er statt einer klaren Meldung beim Start.
    """
    get_master_key()


@lru_cache(maxsize=512)
def _derive(master_key: bytes, tenant_id: str, purpose: bytes) -> bytes:
    """Leitet den mandanten- und zweckspezifischen Schluessel via HKDF-SHA256 ab.

    Der Master-Key wird **nie** direkt zum Verschluesseln benutzt. Gruende:

    * Mandantentrennung — ein kompromittierter abgeleiteter Schluessel oeffnet
      nur die Daten eines Mandanten.
    * Zwecktrennung — der AES-Schluessel und der HMAC-Schluessel desselben
      Mandanten sind verschieden. Denselben Schluessel fuer zwei kryptografische
      Primitive zu verwenden ist ein klassischer Fehler.

    Salt und Info kommen beide aus der Mandanten-ID: das Salt bindet die
    Extraktionsphase an den Mandanten, die Info die Expansionsphase an Mandant
    **und** Zweck.

    Der Master-Key ist Teil des Cache-Schluessels. Damit trifft ein Wechsel des
    Master-Keys nie auf veraltete abgeleitete Schluessel.
    """
    tenant_bytes = str(tenant_id).encode("utf-8")
    salt = hashlib.sha256(_SALT_NAMESPACE + tenant_bytes).digest()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        info=purpose + b"|" + tenant_bytes,
    )
    return hkdf.derive(master_key)


def derive_tenant_key(tenant_id: str, purpose: bytes = PURPOSE_SEAL) -> bytes:
    """Oeffentlicher Zugang zur Schluesselableitung (Tests, Schluesselrotation)."""
    return _derive(get_master_key(), str(tenant_id), purpose)


def seal(tenant_id: str, plaintext: str) -> bytes:
    """Verschluesselt ``plaintext`` fuer ``tenant_id`` mit AES-256-GCM.

    Drahtformat: ``12-Byte-Nonce || Ciphertext || 16-Byte-Tag``. Den Tag haengt
    ``AESGCM.encrypt`` bereits an den Ciphertext an, deshalb genuegt hier das
    Voranstellen der Nonce.

    Die Mandanten-ID geht als AAD (Associated Data) mit ein. Der Schluessel ist
    zwar ohnehin schon mandantenspezifisch — die AAD ist die zweite, explizite
    Sperre dagegen, dass ein Blob per ``UPDATE`` in die Zeile eines anderen
    Mandanten verschoben und dort als dessen Datum ausgegeben wird.
    """
    key = _derive(get_master_key(), str(tenant_id), PURPOSE_SEAL)
    # os.urandom ist der CSPRNG des Betriebssystems. Bei 12 Byte zufaelliger
    # Nonce liegt die Kollisionsgrenze weit oberhalb dessen, was ein Vault je an
    # Eintraegen erreicht.
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext.encode("utf-8"), str(tenant_id).encode("utf-8")
    )
    return nonce + ciphertext


def open(tenant_id: str, blob: bytes) -> str:  # noqa: A001 - Tresor-Fachbegriff
    """Entschluesselt einen Blob von :func:`seal`; dessen Gegenstueck.

    Der Name verdeckt innerhalb dieses Moduls das eingebaute ``open``. Das ist
    beabsichtigt (Tresor-Sprache: versiegeln/oeffnen) und unschaedlich, weil hier
    keine Dateien geoeffnet werden. Aufrufer, die das Builtin brauchen,
    importieren stattdessen den Alias ``unseal``.

    Schlaegt der GCM-Tag fehl — manipuliertes Byte, falscher Mandant, falscher
    Master-Key — wirft ``cryptography`` ``InvalidTag``. Dieser Fehler wird
    absichtlich **nicht** abgefangen und nicht in einen Rueckgabewert
    umgewandelt: stillschweigend falscher Klartext waere hier die weitaus
    schlimmere Alternative.
    """
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise VaultSealError(
            f"Vault-Blob ist zu kurz: {len(blob)} Byte. Ein gueltiger Blob hat "
            f"mindestens {NONCE_BYTES + TAG_BYTES} Byte (Nonce + Tag)."
        )
    key = _derive(get_master_key(), str(tenant_id), PURPOSE_SEAL)
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, str(tenant_id).encode("utf-8"))
    return plaintext.decode("utf-8")


# Alias fuer Aufrufer, die das Builtin ``open`` im selben Namensraum brauchen.
unseal = open


def cluster_key_hmac(tenant_id: str, normalized_entity: str) -> bytes:
    """Berechnet den Cluster-Schluessel als HMAC-SHA256 (32 Byte).

    Der Schluessel wird **separat** vom AES-Schluessel abgeleitet (anderer
    HKDF-Info-String). Wer den Cluster-Schluessel eines Mandanten erbeutet, kann
    damit Gleichheit pruefen, aber keinen einzigen Blob entschluesseln.

    Erwartet einen bereits normalisierten Wert — die Normalisierung entscheidet,
    was als "dieselbe Entitaet" gilt, und gehoert deshalb in die Fachschicht
    (``service.normalize_entity``), nicht in die Krypto.
    """
    key = _derive(get_master_key(), str(tenant_id), PURPOSE_CLUSTER)
    return hmac.new(key, normalized_entity.encode("utf-8"), hashlib.sha256).digest()
