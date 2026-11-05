"""Separate indexing attempts from parsing attempts and record an indexing summary.
Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('index_attempt_count', sa.Integer(), nullable=False,
                                        server_default='0'))
    op.add_column('documents', sa.Column('index_summary', JSONB(), nullable=False,
                                        server_default=sa.text("'{}'::jsonb")))
    op.create_check_constraint('index_attempt_count', 'documents', 'index_attempt_count >= 0')


def downgrade():
    op.drop_constraint('ck_documents_index_attempt_count', 'documents', type_='check')
    op.drop_column('documents', 'index_summary')
    op.drop_column('documents', 'index_attempt_count')
