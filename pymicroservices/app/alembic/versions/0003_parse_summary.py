"""Document parsing summary for status responses.
Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('parse_summary', JSONB(), nullable=False,
                                        server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column('documents', 'parse_summary')
