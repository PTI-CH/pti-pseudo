"""Verschluesselter Zuordnungs-Tresor (Vault) des PTI-Pseudonymisierers.

Der Vault haelt die Zuordnung Original -> Pseudonym. Das ist der sensibelste
Datenbestand des Systems: er enthaelt genau die Personendaten, die im
pseudonymisierten Dokument entfernt wurden. Ohne ihn gaebe es kein
``documents:reverse``.

Aufbau des Pakets:

* ``crypto``     — Schluesselableitung, AES-256-GCM, Cluster-HMAC
* ``models``     — SQLAlchemy-2.0-Tabellen nach Fachkonzept Paragraph 3
* ``pseudonyms`` — Format und Parser der Ersatzsymbole
* ``service``    — fachliche Operationen auf einem Vault
"""

from backend.vault.crypto import (
    VaultKeyError,
    VaultSealError,
    cluster_key_hmac,
    ensure_master_key,
    seal,
    unseal,
)
from backend.vault.models import (
    Base,
    CustomTerm,
    Job,
    MappingEntry,
    Tenant,
    UsageEvent,
    Vault,
)
from backend.vault.pseudonyms import format_pseudonym, parse_pseudonym
from backend.vault.service import (
    delete_vault,
    get_or_create_pseudonym,
    list_entries,
    normalize_entity,
    reverse,
)

__all__ = [
    "Base",
    "CustomTerm",
    "Job",
    "MappingEntry",
    "Tenant",
    "UsageEvent",
    "Vault",
    "VaultKeyError",
    "VaultSealError",
    "cluster_key_hmac",
    "delete_vault",
    "ensure_master_key",
    "format_pseudonym",
    "get_or_create_pseudonym",
    "list_entries",
    "normalize_entity",
    "parse_pseudonym",
    "reverse",
    "seal",
    "unseal",
]
