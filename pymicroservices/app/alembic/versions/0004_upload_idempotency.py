"""Project-scoped upload idempotency keys.
Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('upload_idempotency_key', sa.String(128), nullable=True))
    op.create_unique_constraint('uq_documents_project_upload_key', 'documents', ['project_id', 'upload_idempotency_key'])


def downgrade():
    op.drop_constraint('uq_documents_project_upload_key', 'documents', type_='unique')
    op.drop_column('documents', 'upload_idempotency_key')
