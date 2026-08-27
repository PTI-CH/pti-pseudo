"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Vor dem Committen durchsehen — ``--autogenerate`` irrt verlaesslich an zwei
Stellen (siehe ``migrations/README``):

* ``postgresql.JSONB(astext_type=Text())`` braucht ``sa.Text()``; ohne das
  Praefix fehlt der Import und die Datei ist nicht importierbar.
* ``with op.batch_alter_table(...)`` um ein ``create_index`` stammt aus einem
  Lauf gegen SQLite und gehoert auf PostgreSQL nicht dazu.

Und die Frage, die vor jedem ``drop_``/``alter_`` zu beantworten ist: Was
passiert damit auf der laufenden Produktionsdatenbank? ``mapping_entries``
zu verlieren heisst, jedes umkehrbar pseudonymisierte Dokument dauerhaft
nicht mehr zurueckfuehren zu koennen.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# Von Alembic gelesene Kennungen.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    """Zieht das Schema auf diese Revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Nimmt diese Revision zurueck."""
    ${downgrades if downgrades else "pass"}
