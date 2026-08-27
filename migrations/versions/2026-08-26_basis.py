"""Basis: der Ist-Zustand des Schemas am 2026-08-26.

**Diese Migration wird auf der Produktionsdatenbank NIE ausgefuehrt.** Sie
beschreibt, was dort bereits steht — angelegt von ``backend.db.init_db``
(``create_all``) und ergaenzt von ``backend.db.ensure_schema``. Der Bestand
kommt nicht ueber ``upgrade`` in die Versionsverwaltung, sondern ueber
``stamp``::

    python scripts/alembic_stempeln.py          # prueft und stempelt
    python -m alembic stamp head                # dasselbe, ungeprueft

``upgrade`` gilt ausschliesslich fuer eine **leere** Datenbank: ein frisches
Entwicklungssystem, ein Umzug, ein Wiederaufbau. Laeuft es versehentlich gegen
eine bestehende, bricht es mit einer Erklaerung ab (siehe :func:`upgrade`) —
und selbst ohne diese Pruefung waere PostgreSQLs DDL transaktional: das erste
``CREATE TABLE`` scheiterte und nichts bliebe zurueck.

``downgrade`` gibt es bewusst nicht. Der Rueckbau dieser Revision waere ein
``DROP TABLE mapping_entries`` und damit der Verlust jeder Zuordnung
Original -> Pseudonym. Verschluesselt, ja, aber unwiederbringlich: ohne sie
laesst sich kein umkehrbar pseudonymisiertes Dokument je zurueckfuehren.

Revision ID: 20260826_basis
Revises: keine
Create Date: 2026-08-26

Handkorrekturen an der Ausgabe von ``--autogenerate``:

1. ``postgresql.JSONB(astext_type=Text())`` -> ``sa.Text()``. Alembic schreibt
   ``Text()`` ohne Praefix und ohne passenden Import; die Datei war so nicht
   einmal importierbar (``NameError``).
2. ``with op.batch_alter_table(...)`` um jedes ``create_index`` entfernt. Der
   Tabellenumbau von ``render_as_batch`` gehoert zu spaeteren *Aenderungen* auf
   SQLite, nicht zum erstmaligen Anlegen.
3. ``downgrade`` von ``drop_table``/``drop_index`` auf eine Verweigerung
   umgestellt (Begruendung oben).
4. Sicherung am Anfang von ``upgrade``: laeuft es gegen eine Datenbank, die die
   Tabellen schon hat, nennt die Meldung den richtigen Befehl.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

# Von Alembic gelesene Kennungen. Die Revision traegt bewusst einen lesbaren
# Namen statt eines Zufallshex: sie steht spaeter in ``alembic_version`` und
# ist dort das Einzige, woran man den Stand einer Datenbank abliest.
revision: str = "20260826_basis"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

#: JSON-Spaltentyp wie in ``backend.vault.models.JSONVariant``: echtes ``JSONB``
#: auf PostgreSQL, generisches ``JSON`` sonst. Hier ausgeschrieben statt aus dem
#: Modell importiert — eine Migration beschreibt einen *vergangenen* Zustand und
#: darf sich nicht mit den Modellen mitbewegen.
_JSON_VARIANTE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)

#: Tabelle, an der sich erkennen laesst, ob das Schema schon steht.
_KRONZEUGE = "tenants"


def _schema_steht_schon() -> bool:
    """Meldet, ob die Datenbank die Tabellen dieser Revision bereits hat.

    Im Offline-Modus (``alembic upgrade --sql``) gibt es keine Verbindung, die
    man befragen koennte — dort wird nur SQL erzeugt, also nie etwas
    veraendert, und die Antwort ist ``False``.

    Returns
    -------
    bool
        ``True``, wenn die Tabelle ``tenants`` bereits existiert.
    """
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(_KRONZEUGE)


def upgrade() -> None:
    """Legt das Schema an — nur auf einer leeren Datenbank.

    Raises
    ------
    RuntimeError
        Wenn die Tabellen bereits existieren. Dann ist ``stamp`` gemeint und
        nicht ``upgrade``; die Meldung sagt das.
    """
    if _schema_steht_schon():
        raise RuntimeError(
            "Diese Datenbank hat die Tabellen der Basisrevision bereits. Ein "
            "bestehender Bestand wird GESTEMPELT, nicht migriert: "
            "python scripts/alembic_stempeln.py . Siehe CLAUDE.md, Abschnitt "
            "Schemaaenderungen."
        )

    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=150), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Eindeutiger Index, nicht bloss eindeutige Spalte: er ist zugleich der
    # Zugriffspfad der Schluesselpruefung bei jedem /api/ext/v1-Aufruf.
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"], unique=False)

    op.create_table(
        "custom_terms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.String(length=500), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("match_mode", sa.String(length=20), nullable=False),
        sa.Column("replacement", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_terms_tenant_id", "custom_terms", ["tenant_id"], unique=False
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("quality_json", _JSON_VARIANTE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eingabe_blob", sa.LargeBinary(), nullable=True),
        sa.Column("dateiname", sa.String(length=255), nullable=True),
        sa.Column("parameter_json", _JSON_VARIANTE, nullable=True),
        sa.Column("ergebnis_blob", sa.LargeBinary(), nullable=True),
        sa.Column("fortschritt", sa.Integer(), nullable=True),
        sa.Column("gesamt", sa.Integer(), nullable=True),
        sa.Column("fehler", sa.Text(), nullable=True),
        sa.Column("aktor", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freigaben", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_aktor", "jobs", ["aktor"], unique=False)
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"], unique=False)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=150), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("entity_counts", _JSON_VARIANTE, nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_events_tenant_id", "usage_events", ["tenant_id"], unique=False
    )

    op.create_table(
        "vaults",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vaults_tenant_id", "vaults", ["tenant_id"], unique=False)

    op.create_table(
        "mapping_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("original_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("pseudonym", sa.String(length=100), nullable=False),
        sa.Column("cluster_key_hmac", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vault_id"], ["vaults.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mapping_entries_cluster_key_hmac",
        "mapping_entries",
        ["cluster_key_hmac"],
        unique=False,
    )
    op.create_index(
        "ix_mapping_entries_tenant_id", "mapping_entries", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_mapping_entries_vault_id", "mapping_entries", ["vault_id"], unique=False
    )
    # Die beiden Zusicherungen des Vaults, datenbankseitig: ein Original hat je
    # Tresor genau ein Pseudonym, ein Pseudonym steht fuer genau ein Original.
    op.create_index(
        "uq_mapping_entries_vault_cluster",
        "mapping_entries",
        ["vault_id", "cluster_key_hmac"],
        unique=True,
    )
    op.create_index(
        "uq_mapping_entries_vault_pseudonym",
        "mapping_entries",
        ["vault_id", "pseudonym"],
        unique=True,
    )


def downgrade() -> None:
    """Gibt es bewusst nicht — und das ist der Punkt.

    Raises
    ------
    RuntimeError
        Immer. Der Rueckbau dieser Revision loeschte ``mapping_entries`` und
        damit jede Zuordnung Original -> Pseudonym. Ein Restore aus dem Backup
        braechte sie nur bis zum letzten Dump zurueck; jedes danach umkehrbar
        pseudonymisierte Dokument waere dauerhaft nicht mehr zurueckfuehrbar.
        Wer eine Testdatenbank wirklich leeren will, loescht sie — sichtbar und
        mit Absicht, nicht ueber ein ``alembic downgrade``.
    """
    raise RuntimeError(
        "Kein Rueckbau der Basisrevision. Er wuerde mapping_entries loeschen "
        "und damit jede Zuordnung Original -> Pseudonym unwiederbringlich "
        "vernichten. Eine Testdatenbank wird geloescht, nicht "
        "heruntermigriert."
    )
