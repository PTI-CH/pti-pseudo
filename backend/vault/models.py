"""SQLAlchemy-2.0-Tabellen des Vaults (Fachkonzept Paragraph 3, Phase-1-Zuschnitt).

Portabilitaet PostgreSQL <-> SQLite
-----------------------------------
Produktiv laeuft PostgreSQL. Die Tests laufen gegen SQLite in-memory, weil ein
Testlauf ohne laufenden Datenbankdienst auskommen soll — die Alternative waere,
dass die Vault-Tests bei jedem Entwickler zuerst an einer fehlenden DB scheitern.
Drei Stellen mussten dafuer portabel gehalten werden:

1. **UUID** — ``sqlalchemy.Uuid`` statt des PostgreSQL-eigenen ``UUID``. Der
   generische Typ rendert auf PostgreSQL das native ``UUID`` und auf SQLite ein
   ``CHAR(32)``; auf Python-Seite bleibt es beidseitig ``uuid.UUID``. Damit
   entfaellt die sonst uebliche Kruecke ``String(36)``.
2. **JSONB** — ``JSON().with_variant(JSONB(), "postgresql")``. PostgreSQL bekommt
   echtes ``JSONB`` (indizierbar, binaer gespeichert), SQLite den generischen
   JSON-Typ, der als Text serialisiert. Fachlich wird von diesen Spalten in
   Phase 1 nur gelesen/geschrieben, nicht in ihnen gesucht — die Variante
   kostet also nichts.
3. **Zeitstempel** — ``DateTime(timezone=True)`` mit Vorgabe aus Python
   (``datetime.now(UTC)``) statt ``server_default=func.now()``. SQLite kennt
   keine Zeitzonen; ein serverseitiges ``now()`` haette dort naive Werte
   geliefert und die Tests von der DB-Uhr abhaengig gemacht.

Die Bytes-Spalten brauchen keine Sonderbehandlung: ``LargeBinary`` wird auf
PostgreSQL zu ``BYTEA`` und auf SQLite zu ``BLOB``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: JSON-Spaltentyp: JSONB auf PostgreSQL, generisches JSON sonst. Siehe Modulkopf.
JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    """Zeitstempel in UTC, zeitzonenbewusst.

    Alle Zeitstempel des Vaults sind UTC. Lokalzeit entsteht erst in der
    Darstellung — sonst wandern Sommerzeitspruenge in die Aufbewahrungsfristen.
    """
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    """Primaerschluessel als UUID4.

    Bewusst client-seitig erzeugt und nicht als DB-Sequenz: die ID steht damit
    schon vor dem ``INSERT`` fest (Referenzen ohne Zwischen-Flush), und
    fortlaufende Zahlen wuerden nach aussen verraten, wie viele Zuordnungen ein
    Mandant hat.
    """
    return uuid.uuid4()


class Base(DeclarativeBase):
    """Gemeinsame Deklarationsbasis aller Vault-Tabellen.

    ``backend.db.init_db`` legt genau die hier registrierten Tabellen an.
    """


class Tenant(Base):
    """Mandant. Wurzel der Datentrennung.

    Aus der Mandanten-ID leitet sich der Verschluesselungsschluessel ab (siehe
    ``crypto._derive``). Sie ist damit nicht nur ein Fremdschluessel, sondern
    Teil des Kryptomaterials — eine Mandanten-ID nachtraeglich zu aendern macht
    alle Blobs dieses Mandanten unlesbar.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    vaults: Mapped[list[Vault]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Tracebacks."""
        return f"<Tenant {self.name!r}>"


class Vault(Base):
    """Ein Zuordnungs-Tresor, ueblicherweise ein Fall oder ein Dokumentenstapel.

    Der Vault ist die Einheit, in der Pseudonyme stabil sind: derselbe Name
    bekommt im selben Vault immer dasselbe Pseudonym, in einem anderen Vault
    bewusst ein anderes. Zwei Faelle sollen sich nicht ueber ein gemeinsames
    ``[[PERSON_3]]`` verknuepfen lassen.

    ``expires_at`` traegt die Aufbewahrungsfrist. Das Loeschen selbst erledigt
    ein Aufraeumlauf ueber ``service.delete_vault``, nicht die Datenbank.
    """

    __tablename__ = "vaults"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="vaults")
    entries: Mapped[list[MappingEntry]] = relationship(
        back_populates="vault", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Tracebacks."""
        return f"<Vault {self.label!r}>"


class MappingEntry(Base):
    """Die eigentliche Zuordnung Original -> Pseudonym.

    Spaltenweise begruendet:

    * ``original_ciphertext`` — AES-256-GCM, Format ``Nonce || Ciphertext || Tag``
      (siehe ``crypto.seal``). Der Klartext steht nirgends in dieser Tabelle.
    * ``pseudonym`` — **bewusst Klartext**. Es ist das Ersatzsymbol, das ohnehin
      im ausgelieferten Dokument steht; es zu verschluesseln haette keinen
      Schutzwert, wuerde aber die Rueckfuehrung unnoetig teuer machen.
    * ``cluster_key_hmac`` — HMAC-SHA256 des normalisierten Originals unter einem
      mandantenspezifischen Schluessel. Erlaubt den Gleichheits-Lookup, ohne den
      Klarnamen preiszugeben; die ausfuehrliche Begruendung steht im Kopf von
      ``crypto.py``.

    Die beiden Unique-Constraints sind die Datenbank-seitige Absicherung der
    zentralen Zusage des Vaults — sie halten auch dann, wenn zwei Anfragen
    gleichzeitig denselben Namen anlegen wollen:

    * ``(vault_id, cluster_key_hmac)`` — ein Original hat je Vault genau ein
      Pseudonym (Stabilitaet).
    * ``(vault_id, pseudonym)`` — ein Pseudonym steht je Vault fuer genau ein
      Original (Eindeutigkeit der Rueckfuehrung).
    """

    __tablename__ = "mapping_entries"
    __table_args__ = (
        Index(
            "uq_mapping_entries_vault_cluster",
            "vault_id",
            "cluster_key_hmac",
            unique=True,
        ),
        Index(
            "uq_mapping_entries_vault_pseudonym", "vault_id", "pseudonym", unique=True
        ),
        # Nicht-eindeutiger Index fuer den Lookup-Pfad von
        # ``get_or_create_pseudonym``. Der Unique-Index oben wuerde ihn auf
        # PostgreSQL zwar mit abdecken, aber der Zugriff erfolgt oft ohne
        # vorangestelltes ``vault_id`` (mandantenweite Auswertungen).
        Index("ix_mapping_entries_cluster_key_hmac", "cluster_key_hmac"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vault_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("vaults.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    original_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pseudonym: Mapped[str] = mapped_column(String(100), nullable=False)
    cluster_key_hmac: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    vault: Mapped[Vault] = relationship(back_populates="entries")

    def __repr__(self) -> str:
        """Kurzform ohne jeden Personenbezug — siehe Kommentar."""
        # Absichtlich ohne Klartext und ohne Ciphertext: dieses ``repr`` landet
        # in Logs und Tracebacks.
        return f"<MappingEntry {self.entity_type} {self.pseudonym}>"


class CustomTerm(Base):
    """Mandanteneigene Begriffe, die zusaetzlich erkannt werden sollen.

    Deckt ab, was ein statistisches NER-Modell nicht wissen kann: interne
    Projektnamen, Aktenzeichen-Formate, Ortsbezeichnungen einer Gemeinde.

    ``match_mode`` steuert den Abgleich (z. B. ``exact``, ``ci`` fuer
    Gross-/Kleinschreibung ignorierend, ``regex``). Bewusst als Zeichenkette und
    nicht als DB-Enum: neue Modi sollen ohne Schemamigration hinzukommen
    koennen, und die zulaessigen Werte prueft die Fachschicht.
    """

    __tablename__ = "custom_terms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    match_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="exact")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Job(Base):
    """Ein Verarbeitungsauftrag.

    In Phase 1 laeuft die Verarbeitung synchron; der Datensatz dient hier der
    Nachvollziehbarkeit und traegt in ``quality_json`` die Guetekennzahlen des
    Laufs (Trefferzahlen je Entitaetstyp, Konfidenzverteilung). Ab Phase 2
    haengen daran die asynchronen Jobs.

    ``quality_json`` enthaelt ausschliesslich Kennzahlen — keine Textausschnitte
    und keine erkannten Werte.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    quality_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UsageEvent(Base):
    """Nutzungsereignis fuer Abrechnung und Kapazitaetsplanung.

    ACHTUNG — diese Tabelle enthaelt **nur Metadaten und Zahlen, niemals Inhalte
    und niemals Dateinamen.** Das ist keine Stilfrage, sondern der Grund, warum
    sie ueberhaupt existieren darf: sie ist die einzige Tabelle, die man fuer
    Auswertungen breit lesbar machen kann, und das nur, solange sie nichts
    Schuetzenswertes enthaelt.

    Konkret verboten: Dokumententitel, Dateinamen, Textausschnitte, erkannte
    Entitaetswerte, Pseudonyme. Erlaubt: Wer (``username``), was (``action``),
    wie viele (``entity_counts`` je Typ, ``char_count``), wann.

    Ein Dateiname ist bereits ein Personendatum — "Strafanzeige_Meier_2026.docx"
    verraet mehr als der Inhalt mancher Absaetze.
    """

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stabiler Schluessel des TIP-Hub-SSO ist ``username``, nicht die UUID.
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    # Reine Zaehler je Entitaetstyp, z. B. {"PERSON": 12, "LOCATION": 3}.
    entity_counts: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "Base",
    "CustomTerm",
    "Job",
    "MappingEntry",
    "Tenant",
    "UsageEvent",
    "Vault",
]
