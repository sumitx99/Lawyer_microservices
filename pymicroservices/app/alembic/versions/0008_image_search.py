"""Full-text search over model-derived image descriptions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('image_rag_vector_table', sa.Column('tsv', postgresql.TSVECTOR(),
        sa.Computed("to_tsvector('simple'::regconfig, description)", persisted=True), nullable=False))
    op.create_index('ix_image_rag_vector_table_tsv', 'image_rag_vector_table', ['tsv'], postgresql_using='gin')


def downgrade():
    op.drop_index('ix_image_rag_vector_table_tsv', table_name='image_rag_vector_table')
    op.drop_column('image_rag_vector_table', 'tsv')
