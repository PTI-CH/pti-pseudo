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
    Text,
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


def as_utc(value: datetime | None) -> datetime | None:
    """Macht einen gelesenen Zeitstempel zuverlaessig zeitzonenbewusst.

    SQLite speichert ``DateTime(timezone=True)`` ohne Zone und liefert beim
    Lesen ein naives ``datetime`` zurueck; PostgreSQL liefert ein bewusstes.
    Ein Vergleich zwischen beiden Sorten wirft ``TypeError`` — genau der
    Fehler, der erst im Testlauf gegen SQLite auffaellt. Deshalb hier
    einmalig geradegezogen, statt an jeder Vergleichsstelle.

    Oeffentlich, weil das nicht nur die Modelle betrifft: wer einen gelesenen
    Zeitstempel serialisiert, muss ihn vorher hier durchreichen. Ein naiver
    Wert wird zu ISO **ohne** Zonenangabe, und ein Browser liest den dann als
    Lokalzeit — im Sommer zwei Stunden daneben.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


#: Alter, modulinterner Name. Bleibt als Alias bestehen, damit bestehende
#: Aufrufer (und Tests) nicht brechen — neuer Code nimmt ``as_utc``.
_as_utc = as_utc


def iso_utc(value: datetime | None) -> str | None:
    """Gelesener Zeitstempel als ISO-8601 **mit** Zonenangabe, oder ``None``.

    Die Ergaenzung zu :func:`as_utc` fuer den Weg nach draussen. Ein blosses
    ``value.isoformat()`` auf einem aus SQLite gelesenen Wert schreibt den
    Zeitstempel ohne ``+00:00``; ein Browser liest einen ISO-String ohne Zone
    als **Lokalzeit** und zeigt ihn im Sommer zwei Stunden daneben.

    Steht hier und nicht in einem Router, weil ihn inzwischen mehrere brauchen
    (Vaults, Begriffsliste, API-Schluessel). Jede Serialisierung eines
    gelesenen Zeitstempels geht durch diese Funktion — sonst haengt die
    Korrektheit daran, dass die naechste Antwortform daran denkt.
    """
    value = as_utc(value)
    return value.isoformat() if value is not None else None


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

    ``replacement`` ist das **feste Pseudonym**: statt eines gezaehlten
    ``[[ORGANIZATION_3]]`` bekommt dieser Begriff immer denselben, selbst
    gewaehlten Ersatztext. Gedacht fuer Begriffe, die in jedem Dokument
    wiederkehren und deren Ersatz lesbar bleiben soll. ``None`` heisst:
    Pseudonym wie ueblich vergeben.

    Die Laenge ist auf 100 Zeichen begrenzt, weil der Wert eins zu eins in
    ``MappingEntry.pseudonym`` (``String(100)``) landet — waere sie groesser,
    schluege erst die Datenbank zu, und zwar mitten in einem Dokumentenlauf.
    """

    __tablename__ = "custom_terms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    match_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="exact")
    replacement: Mapped[str | None] = mapped_column(String(100), nullable=True)
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
    #: Die verschluesselte Eingabe. Wird auf ``None`` gesetzt, sobald der
    #: Auftrag endet — bei Erfolg UND bei Fehler. Ein gescheiterter Auftrag
    #: darf keine Personendaten liegenlassen.
    eingabe_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dateiname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Die Wahl des Nutzers beim Anlegen: Kategorien, Modus, KI-Haekchen,
    #: Fundauswahl, bei der Rueckfuehrung die vault_id. Enthaelt KEINE Inhalte
    #: -- eine normale, unverschluesselte JSONB-Spalte, sichtbar in jedem
    #: Datenbankabzug. Die Fundauswahl (``selected_entities``) traegt dabei
    #: NIE den Klartext eines Fundes, nur das Tripel, ueber das
    #: ``pipeline._select`` ihn wiedererkennt (``segment_id``, ``start``,
    #: ``end``) plus die Kategorie: ``warteschlange._ohne_klartext`` streift
    #: alles andere ab, bevor die Zeile entsteht -- selbst wenn ein Aufrufer
    #: (etwa ueber ``/api/ext/v1``) weiterhin ``text`` mitschickt.
    parameter_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    ergebnis_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    fortschritt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gesamt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fehler: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Der Hub-Benutzername oder ``apikey:<id>`` — siehe ``core.deps.current_actor``.
    #: Menschen stehen hier OHNE Praefix; nur Maschinen tragen eines.
    aktor: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Wie oft dieser Auftrag schon als verwaist freigegeben wurde. Ab drei
    #: gilt er als endgueltig gescheitert — sonst wandert ein Dokument, das den
    #: Arbeiter zuverlaessig abstuerzen laesst, fuer immer im Kreis.
    freigaben: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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


class ApiKey(Base):
    """Schluessel einer aufrufenden Anwendung fuer ``/api/ext/v1``.

    Warum in der Datenbank und nicht in der Umgebung: hier sollen *mehrere*
    Aufrufer Schluessel bekommen und wieder verlieren, ohne dass jemand im
    Jelastic-Dashboard eine Variable aendert und den Knoten neu startet. Wer
    diese Aufrufer sind, ist offen und ausdruecklich nicht vorgegeben — der
    Pseudonymisierer bietet eine Schnittstelle an, er verabredet sich nicht
    mit einer bestimmten Anwendung (siehe CLAUDE.md, „Nachbar-Repos").

    Warum nur der Hash: gespeichert wird ausschliesslich ``sha256(token)`` in
    Hex. Der Klartext wird genau einmal gezeigt — bei der Erzeugung. Ein
    langsames Verfahren (bcrypt, Argon2) waere hier ohne Nutzen, weil der Token
    32 zufaellige Bytes traegt und nicht erraten werden kann; SHA-256 haelt den
    Lookup dafuer billig genug, um ihn bei jedem Aufruf zu machen.

    Warum Soft-Delete: ``revoked_at`` statt ``DELETE``. Ein widerrufener
    Schluessel muss in der Liste sichtbar bleiben — sonst laesst sich spaeter
    nicht mehr sagen, welche Anwendung wann Zugriff hatte. ``key_prefix`` haelt
    die ersten 12 Zeichen fest, damit ein Schluessel in Liste und Log
    wiedererkennbar ist, ohne dass der Klartext irgendwo steht.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # Unique-Index: er ist zugleich der Zugriffspfad der Authentisierung —
    # ein Aufruf schlaegt genau eine Zeile nach, nicht die ganze Tabelle.
    key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_by: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        """Nicht widerrufen und nicht abgelaufen.

        Beides ergibt nach aussen denselben Befund (``API_KEY_INVALID``) — wer
        einen ungueltigen Schluessel schickt, soll nicht erfahren, ob er
        widerrufen wurde oder nie existiert hat.
        """
        if self.revoked_at is not None:
            return False
        expires_at = as_utc(self.expires_at)
        return expires_at is None or expires_at > _utcnow()

    def __repr__(self) -> str:
        """Kurzform fuer Logs — mit Praefix, nie mit Hash oder Klartext."""
        return f"<ApiKey {self.label!r} {self.key_prefix}...>"


__all__ = [
    "ApiKey",
    "Base",
    "as_utc",
    "CustomTerm",
    "iso_utc",
    "Job",
    "MappingEntry",
    "Tenant",
    "UsageEvent",
    "Vault",
]
