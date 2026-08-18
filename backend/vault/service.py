"""Fachliche Operationen auf einem Vault.

Die zentrale Zusage dieses Moduls: **Stabilitaet**. Derselbe Name bekommt im
selben Vault immer dasselbe Pseudonym — ueber Dokumentgrenzen und ueber
Sitzungen hinweg. Ohne diese Zusage waere ein Dokumentenstapel nach der
Pseudonymisierung inhaltlich zerstoert: dieselbe Person traege in Akte 1 ein
anderes Symbol als in Akte 2, und der Zusammenhang, um dessentwillen ueberhaupt
pseudonymisiert statt geschwaerzt wird, waere weg.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.vault import crypto
from backend.vault.models import MappingEntry, Vault
from backend.vault.pseudonyms import format_pseudonym, parse_pseudonym

# Whitespace jeder Art (auch geschuetzte Leerzeichen und Zeilenumbrueche aus
# DOCX-Konvertierungen) wird zu einem einfachen Leerzeichen zusammengefasst.
_WHITESPACE = re.compile(r"\s+", re.UNICODE)


def normalize_entity(text: str) -> str:
    """Normalisiert einen Entitaetstext fuer die Cluster-Bildung.

    Schritte, jeder mit einem konkreten Anlass aus echten Dokumenten:

    1. **Unicode-NFKC** — "Andreas" aus einem PDF-Export kann typografische
       Varianten enthalten (z. B. Ligaturen oder Breitenformen), die visuell
       identisch, als Codepoint aber verschieden sind.
    2. **Whitespace zusammenfassen und trimmen** — "Andreas  Meier" mit doppeltem
       Leerzeichen entsteht regelmaessig beim Umbruch in Tabellenzellen.
    3. **Casefold** — "ANDREAS MEIER" in einer Ueberschrift und "Andreas Meier"
       im Fliesstext sind dieselbe Person. ``casefold`` statt ``lower``, weil es
       auch das deutsche Eszett behandelt ("Strasse"/"Straße" bleibt allerdings
       verschieden — das ist bewusst nicht Aufgabe der Normalisierung).

    Was hier **nicht** passiert: Umlaut-Transliteration, Namensteile umsortieren,
    Titel entfernen. Jede dieser Regeln wuerde verschiedene Personen
    zusammenlegen, und eine faelschlich zusammengelegte Zuordnung ist bei der
    Rueckfuehrung nicht mehr zu reparieren — der Klartext des zweiten Namens
    wurde dann nie gespeichert.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    return normalized.casefold()


def _cluster_key(tenant_id, entity_type: str, original_text: str) -> bytes:
    """Cluster-HMAC inklusive Entitaetstyp.

    Der Typ geht in den HMAC-Eingabewert ein, weil ``(vault_id,
    cluster_key_hmac)`` eindeutig ist: derselbe Text als ``PERSON`` und als
    ``ORGANIZATION`` (etwa eine Einzelfirma, die wie ihr Inhaber heisst) soll
    zwei getrennte Zuordnungen ergeben duerfen und nicht am Unique-Constraint
    scheitern.
    """
    payload = f"{str(entity_type).upper()}:{normalize_entity(original_text)}"
    return crypto.cluster_key_hmac(str(tenant_id), payload)


def _next_counter(session: Session, vault: Vault, entity_type: str) -> int:
    """Ermittelt den naechsten freien Zaehler fuer einen Entitaetstyp.

    Abgeleitet aus den vorhandenen Pseudonymen statt aus einer eigenen
    Zaehlerspalte: eine zweite Quelle koennte von den tatsaechlich vergebenen
    Symbolen abweichen, und genau diese Abweichung wuerde erst beim Kollidieren
    des Unique-Constraints auffallen. ``COUNT(*)`` waere aus demselben Grund
    falsch — nach einer Loeschung gaebe es den Zaehler doppelt.
    """
    entity_type = str(entity_type).upper()
    rows = session.execute(
        select(MappingEntry.pseudonym).where(
            MappingEntry.vault_id == vault.id,
            MappingEntry.entity_type == entity_type,
        )
    ).scalars()

    highest = 0
    for pseudonym in rows:
        parsed = parse_pseudonym(pseudonym)
        if parsed is not None and parsed[0] == entity_type:
            highest = max(highest, parsed[1])
    return highest + 1


def get_or_create_pseudonym(
    session: Session, vault: Vault, entity_type: str, original_text: str
) -> str:
    """Liefert das Pseudonym fuer ``original_text`` und legt es noetigenfalls an.

    Ablauf: normalisieren, HMAC bilden, bestehenden Eintrag suchen. Gibt es ihn,
    wird sein Pseudonym zurueckgegeben — **derselbe Name bekommt im selben Vault
    immer dasselbe Pseudonym**. Sonst entsteht ein neuer Eintrag mit dem
    naechsten freien Zaehler dieses Entitaetstyps.

    Gespeichert wird der **originale** Text, nicht der normalisierte: die
    Rueckfuehrung muss die urspruengliche Schreibweise wiederherstellen, nicht
    eine kleingeschriebene Variante. Bei mehreren Schreibweisen gewinnt die
    zuerst gesehene — eine bewusste Entscheidung, denn eine Zuordnung kann nur
    einen Klartext haben.

    ``flush`` am Ende, damit der Eintrag innerhalb derselben Transaktion sofort
    wiedergefunden wird. Das Commit bleibt Sache des Aufrufers: ein Dokument
    wird als Ganzes pseudonymisiert oder gar nicht.
    """
    entity_type = str(entity_type).upper()
    cluster_key = _cluster_key(vault.tenant_id, entity_type, original_text)

    existing = session.execute(
        select(MappingEntry).where(
            MappingEntry.vault_id == vault.id,
            MappingEntry.cluster_key_hmac == cluster_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.pseudonym

    pseudonym = format_pseudonym(
        entity_type, _next_counter(session, vault, entity_type)
    )
    entry = MappingEntry(
        tenant_id=vault.tenant_id,
        vault_id=vault.id,
        entity_type=entity_type,
        original_ciphertext=crypto.seal(str(vault.tenant_id), original_text),
        pseudonym=pseudonym,
        cluster_key_hmac=cluster_key,
    )
    session.add(entry)
    session.flush()
    return pseudonym


def list_entries(
    session: Session, vault: Vault, entity_type: str | None = None
) -> Sequence[MappingEntry]:
    """Gibt die Eintraege eines Vaults zurueck, sortiert nach Anlagezeitpunkt.

    Liefert die Datensaetze mit Ciphertext, nicht die Klartexte. Wer die
    Originale braucht, entschluesselt sie einzeln ueber ``crypto.unseal`` — so
    steht an jeder Aufrufstelle sichtbar, dass dort Klarnamen entstehen.
    """
    statement = select(MappingEntry).where(MappingEntry.vault_id == vault.id)
    if entity_type is not None:
        statement = statement.where(
            MappingEntry.entity_type == str(entity_type).upper()
        )
    statement = statement.order_by(MappingEntry.created_at, MappingEntry.pseudonym)
    return session.execute(statement).scalars().all()


def reverse(session: Session, vault: Vault, text: str) -> str:
    """Ersetzt alle Pseudonyme des Vaults wieder durch die Originale.

    Zwei Eigenschaften, die diese Funktion korrekt machen:

    1. **Laengste Pseudonyme zuerst.** Sonst koennte ein Praefix-Treffer ein
       laengeres Symbol zerlegen — ``[[PERSON_11]]`` duerfte nie als
       ``[[PERSON_1]]`` plus uebrig gebliebenem ``1]]`` behandelt werden. Die
       Klammerform schuetzt hier bereits weitgehend, die Sortierung macht die
       Zusage aber unabhaengig vom konfigurierten Format.
    2. **Ein einziger Durchlauf** ueber ``re.sub`` mit einer Alternation, statt
       einer Kette von ``str.replace``. Bei nacheinander ausgefuehrten
       Ersetzungen koennte ein bereits eingesetzter Klartext, der zufaellig wie
       ein Pseudonym aussieht, in einem spaeteren Durchgang erneut ersetzt
       werden. Ein Durchlauf schliesst das strukturell aus.

    Nicht gefundene Pseudonyme sind kein Fehler: ein Dokument enthaelt selten
    alle Entitaeten eines Vaults.
    """
    entries = (
        session.execute(select(MappingEntry).where(MappingEntry.vault_id == vault.id))
        .scalars()
        .all()
    )
    if not entries:
        return text

    originals = {
        entry.pseudonym: crypto.unseal(
            str(entry.tenant_id), bytes(entry.original_ciphertext)
        )
        for entry in entries
    }

    # Laenge absteigend, bei Gleichstand alphabetisch — damit ist die erzeugte
    # Alternation deterministisch und ein Fehlverhalten reproduzierbar.
    pseudonyms = sorted(originals, key=lambda value: (-len(value), value))
    pattern = re.compile("|".join(re.escape(value) for value in pseudonyms))
    return pattern.sub(lambda match: originals[match.group(0)], text)


def delete_vault(session: Session, vault: Vault) -> int:
    """Loescht einen Vault samt seiner Zuordnungen; gibt deren Anzahl zurueck.

    Die Eintraege verschwinden ueber die ORM-Kaskade der Beziehung
    (``cascade="all, delete-orphan"``) und nicht ueber das ``ON DELETE CASCADE``
    der Datenbank: SQLite erzwingt Fremdschluessel nur bei eingeschaltetem
    ``PRAGMA foreign_keys``. Beim Loeschen des sensibelsten Datenbestands des
    Systems soll nichts von einer Datenbank-Einstellung abhaengen — ein
    verwaister Eintrag waere hier ein Klartext-Leck, das niemand mehr einem
    Vorgang zuordnen kann.

    Kein ``commit`` — das Loeschen gehoert in die Transaktion des Aufrufers, der
    ueblicherweise noch ein ``UsageEvent`` dazuschreibt.
    """
    removed = session.execute(
        select(func.count())
        .select_from(MappingEntry)
        .where(MappingEntry.vault_id == vault.id)
    ).scalar_one()
    session.delete(vault)
    session.flush()
    return int(removed)
