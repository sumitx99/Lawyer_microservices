"""Metaclassifier details: dated, valued and property facts of deeds and contracts.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('unstructured_metadata', sa.Column('details', postgresql.JSONB(astext_type=sa.Text()),
                                                     server_default=sa.text("'{}'::jsonb"), nullable=False))


def downgrade():
    op.drop_column('unstructured_metadata', 'details')
