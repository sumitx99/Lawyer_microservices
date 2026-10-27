"""Project descriptions and owner listing index.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('data_fabric', sa.Column('description', sa.Text(), nullable=True))
    op.create_index('ix_data_fabric_created_by', 'data_fabric', ['created_by', 'created_at'])


def downgrade():
    op.drop_index('ix_data_fabric_created_by', table_name='data_fabric')
    op.drop_column('data_fabric', 'description')
