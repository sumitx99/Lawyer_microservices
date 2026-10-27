"""Project-scoped upload idempotency keys.
Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('upload_idempotency_key', sa.String(128), nullable=True))
    op.create_unique_constraint('uq_documents_project_upload_key', 'documents', ['project_id', 'upload_idempotency_key'])


def downgrade():
    op.drop_constraint('uq_documents_project_upload_key', 'documents', type_='unique')
    op.drop_column('documents', 'upload_idempotency_key')
