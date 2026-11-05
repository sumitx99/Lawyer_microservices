"""Folder pipeline tables from the design doc, added alongside the per-document tables.

project (data_fabric) -> folder (unstructured_thread) -> file (unstructured_data, with status,
updated_at, attempts, error, owner and lease per stage) -> text_nodes, node_relationships,
image_rag_vector_table and unstructured_metadata.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

TOUCHED = ('data_fabric', 'unstructured_thread', 'unstructured_data', 'text_nodes',
           'image_rag_vector_table', 'unstructured_metadata')


def upgrade():
    op.create_table('data_fabric',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_fabric'))
    )
    op.create_table('unstructured_thread',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='UPLOADING', nullable=False),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('jurisdiction', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('UPLOADING','PROCESSING','COMPLETED','PARTIAL','FAILED')", name=op.f('ck_unstructured_thread_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unstructured_thread'))
    )
    op.create_index('ix_unstructured_thread_created_by', 'unstructured_thread', ['created_by', 'created_at'], unique=False)
    op.create_table('fabric_unstructured_thread',
    sa.Column('fabric_id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['fabric_id'], ['data_fabric.id'], name=op.f('fk_fabric_unstructured_thread_fabric_id_data_fabric'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['thread_id'], ['unstructured_thread.id'], name=op.f('fk_fabric_unstructured_thread_thread_id_unstructured_thread'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('fabric_id', 'thread_id', name=op.f('pk_fabric_unstructured_thread'))
    )
    op.create_table('unstructured_data',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('file_name', sa.Text(), nullable=False),
    sa.Column('file_type', sa.String(length=16), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('byte_count', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('s3_path', sa.Text(), nullable=False),
    sa.Column('upload_status', sa.String(length=16), server_default='RUNNING', nullable=False),
    sa.Column('upload_error', sa.Text(), nullable=True),
    sa.Column('s3_path_tomarkdown', sa.Text(), nullable=True),
    sa.Column('s3_path_images', sa.Text(), nullable=True),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('language', sa.String(length=32), nullable=True),
    sa.Column('processing_summary', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('standardization_status', sa.String(length=16), nullable=True),
    sa.Column('standardization_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('standardization_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('standardization_error', sa.Text(), nullable=True),
    sa.Column('standardization_owner', sa.String(length=128), nullable=True),
    sa.Column('standardization_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('markdown_status', sa.String(length=16), nullable=True),
    sa.Column('markdown_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('markdown_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('markdown_error', sa.Text(), nullable=True),
    sa.Column('markdown_owner', sa.String(length=128), nullable=True),
    sa.Column('markdown_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('image_status', sa.String(length=16), nullable=True),
    sa.Column('image_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('image_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('image_error', sa.Text(), nullable=True),
    sa.Column('image_owner', sa.String(length=128), nullable=True),
    sa.Column('image_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metaclassifier_status', sa.String(length=16), nullable=True),
    sa.Column('metaclassifier_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metaclassifier_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('metaclassifier_error', sa.Text(), nullable=True),
    sa.Column('metaclassifier_owner', sa.String(length=128), nullable=True),
    sa.Column('metaclassifier_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('parquet_status', sa.String(length=16), nullable=True),
    sa.Column('parquet_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('parquet_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('parquet_error', sa.Text(), nullable=True),
    sa.Column('parquet_owner', sa.String(length=128), nullable=True),
    sa.Column('parquet_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('document', 'presentation', 'spreadsheet', 'csv', 'image', 'text')", name=op.f('ck_unstructured_data_category')),
    sa.CheckConstraint("coalesce(image_status = 'RUNNING', false) = (image_owner IS NOT NULL AND image_lease_expires_at IS NOT NULL)", name=op.f('ck_unstructured_data_image_running_owner')),
    sa.CheckConstraint("coalesce(markdown_status = 'RUNNING', false) = (markdown_owner IS NOT NULL AND markdown_lease_expires_at IS NOT NULL)", name=op.f('ck_unstructured_data_markdown_running_owner')),
    sa.CheckConstraint("coalesce(metaclassifier_status = 'RUNNING', false) = (metaclassifier_owner IS NOT NULL AND metaclassifier_lease_expires_at IS NOT NULL)", name=op.f('ck_unstructured_data_metaclassifier_running_owner')),
    sa.CheckConstraint("coalesce(parquet_status = 'RUNNING', false) = (parquet_owner IS NOT NULL AND parquet_lease_expires_at IS NOT NULL)", name=op.f('ck_unstructured_data_parquet_running_owner')),
    sa.CheckConstraint("coalesce(standardization_status = 'RUNNING', false) = (standardization_owner IS NOT NULL AND standardization_lease_expires_at IS NOT NULL)", name=op.f('ck_unstructured_data_standardization_running_owner')),
    sa.CheckConstraint("file_type IN ('pdf', 'docx', 'doc', 'txt', 'pptx', 'xlsx', 'csv', 'jpg', 'png')", name=op.f('ck_unstructured_data_file_type')),
    sa.CheckConstraint("image_status IN ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')", name=op.f('ck_unstructured_data_image_status')),
    sa.CheckConstraint("markdown_status IN ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')", name=op.f('ck_unstructured_data_markdown_status')),
    sa.CheckConstraint("metaclassifier_status IN ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')", name=op.f('ck_unstructured_data_metaclassifier_status')),
    sa.CheckConstraint("parquet_status IN ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')", name=op.f('ck_unstructured_data_parquet_status')),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f('ck_unstructured_data_sha256')),
    sa.CheckConstraint("standardization_status IN ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')", name=op.f('ck_unstructured_data_standardization_status')),
    sa.CheckConstraint("upload_status IN ('RUNNING','COMPLETED','FAILED')", name=op.f('ck_unstructured_data_upload_status')),
    sa.CheckConstraint('byte_count >= 0 AND (page_count IS NULL OR page_count >= 0)', name=op.f('ck_unstructured_data_nonnegative')),
    sa.CheckConstraint('image_attempts >= 0', name=op.f('ck_unstructured_data_image_attempts')),
    sa.CheckConstraint('markdown_attempts >= 0', name=op.f('ck_unstructured_data_markdown_attempts')),
    sa.CheckConstraint('metaclassifier_attempts >= 0', name=op.f('ck_unstructured_data_metaclassifier_attempts')),
    sa.CheckConstraint('parquet_attempts >= 0', name=op.f('ck_unstructured_data_parquet_attempts')),
    sa.CheckConstraint('standardization_attempts >= 0', name=op.f('ck_unstructured_data_standardization_attempts')),
    sa.ForeignKeyConstraint(['thread_id'], ['unstructured_thread.id'], name=op.f('fk_unstructured_data_thread_id_unstructured_thread'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unstructured_data')),
    sa.UniqueConstraint('id', 'thread_id', name='uq_unstructured_data_id_thread'),
    sa.UniqueConstraint('s3_path', name=op.f('uq_unstructured_data_s3_path')),
    sa.UniqueConstraint('thread_id', 'sha256', name='uq_unstructured_data_thread_sha256')
    )
    op.create_index('ix_unstructured_data_image_running', 'unstructured_data', ['image_lease_expires_at'], unique=False, postgresql_where=sa.text("image_status = 'RUNNING'"))
    op.create_index('ix_unstructured_data_image_waiting', 'unstructured_data', ['image_updated_at'], unique=False, postgresql_where=sa.text("image_status = 'WAITING'"))
    op.create_index('ix_unstructured_data_markdown_running', 'unstructured_data', ['markdown_lease_expires_at'], unique=False, postgresql_where=sa.text("markdown_status = 'RUNNING'"))
    op.create_index('ix_unstructured_data_markdown_waiting', 'unstructured_data', ['markdown_updated_at'], unique=False, postgresql_where=sa.text("markdown_status = 'WAITING'"))
    op.create_index('ix_unstructured_data_metaclassifier_running', 'unstructured_data', ['metaclassifier_lease_expires_at'], unique=False, postgresql_where=sa.text("metaclassifier_status = 'RUNNING'"))
    op.create_index('ix_unstructured_data_metaclassifier_waiting', 'unstructured_data', ['metaclassifier_updated_at'], unique=False, postgresql_where=sa.text("metaclassifier_status = 'WAITING'"))
    op.create_index('ix_unstructured_data_parquet_running', 'unstructured_data', ['parquet_lease_expires_at'], unique=False, postgresql_where=sa.text("parquet_status = 'RUNNING'"))
    op.create_index('ix_unstructured_data_parquet_waiting', 'unstructured_data', ['parquet_updated_at'], unique=False, postgresql_where=sa.text("parquet_status = 'WAITING'"))
    op.create_index('ix_unstructured_data_standardization_running', 'unstructured_data', ['standardization_lease_expires_at'], unique=False, postgresql_where=sa.text("standardization_status = 'RUNNING'"))
    op.create_index('ix_unstructured_data_standardization_waiting', 'unstructured_data', ['standardization_updated_at'], unique=False, postgresql_where=sa.text("standardization_status = 'WAITING'"))
    op.create_table('image_rag_vector_table',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('doc_id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('image_index', sa.Integer(), nullable=False),
    sa.Column('s3_path', sa.Text(), nullable=False),
    sa.Column('page_no', sa.Integer(), nullable=True),
    sa.Column('image_type', sa.String(length=16), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('embedding', Vector(1024), nullable=False),
    sa.Column('embedding_model', sa.Text(), server_default='BAAI/bge-m3', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("image_type IN ('table','graph','image')", name=op.f('ck_image_rag_vector_table_image_type')),
    sa.CheckConstraint('image_index >= 0 AND (page_no IS NULL OR page_no >= 1)', name=op.f('ck_image_rag_vector_table_positions')),
    sa.ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'], name=op.f('fk_image_rag_vector_table_doc_id_unstructured_data'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_image_rag_vector_table')),
    sa.UniqueConstraint('doc_id', 'image_index', name='uq_image_rag_vector_table_doc_image')
    )
    op.create_index('ix_image_rag_vector_table_embedding', 'image_rag_vector_table', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_image_rag_vector_table_thread', 'image_rag_vector_table', ['thread_id'], unique=False)
    op.create_table('text_nodes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('doc_id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('total_chunks', sa.Integer(), nullable=False),
    sa.Column('node_type', sa.String(length=16), server_default='text', nullable=False),
    sa.Column('heading_path', sa.Text(), server_default='', nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('page_start', sa.Integer(), nullable=True),
    sa.Column('page_end', sa.Integer(), nullable=True),
    sa.Column('table_id', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('ts_config', postgresql.REGCONFIG(), server_default=sa.text("'simple'::regconfig"), nullable=False),
    sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector(ts_config, coalesce(heading_path, '') || ' ' || content)", persisted=True), nullable=False),
    sa.Column('embedding', Vector(1024), nullable=False),
    sa.Column('embedding_model', sa.Text(), server_default='BAAI/bge-m3', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("node_type IN ('text','table')", name=op.f('ck_text_nodes_node_type')),
    sa.CheckConstraint('chunk_index >= 0 AND total_chunks > chunk_index AND token_count >= 0', name=op.f('ck_text_nodes_counts')),
    sa.CheckConstraint('page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)', name=op.f('ck_text_nodes_page_end')),
    sa.CheckConstraint('page_start IS NULL OR page_start >= 1', name=op.f('ck_text_nodes_page_start')),
    sa.ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'], name=op.f('fk_text_nodes_doc_id_unstructured_data'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_text_nodes')),
    sa.UniqueConstraint('doc_id', 'chunk_index', name='uq_text_nodes_doc_chunk')
    )
    op.create_index('ix_text_nodes_embedding', 'text_nodes', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_text_nodes_table', 'text_nodes', ['doc_id', 'table_id'], unique=False, postgresql_where=sa.text('table_id IS NOT NULL'))
    op.create_index('ix_text_nodes_thread', 'text_nodes', ['thread_id'], unique=False)
    op.create_index('ix_text_nodes_tsv', 'text_nodes', ['tsv'], unique=False, postgresql_using='gin')
    op.create_table('unstructured_metadata',
    sa.Column('doc_id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('document_type', sa.Text(), nullable=True),
    sa.Column('jurisdiction', sa.Text(), nullable=True),
    sa.Column('court', sa.Text(), nullable=True),
    sa.Column('case_number', sa.Text(), nullable=True),
    sa.Column('parties', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('judges', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('decision_date', sa.Date(), nullable=True),
    sa.Column('citation', sa.Text(), nullable=True),
    sa.Column('statutes_cited', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('classification', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('language', sa.String(length=32), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('title_embedding', Vector(1024), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'], name=op.f('fk_unstructured_metadata_doc_id_unstructured_data'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('doc_id', name=op.f('pk_unstructured_metadata'))
    )
    op.create_index('ix_unstructured_metadata_filters', 'unstructured_metadata', ['jurisdiction', 'court', 'decision_date'], unique=False)
    op.create_index('ix_unstructured_metadata_thread', 'unstructured_metadata', ['thread_id'], unique=False)
    op.create_index('ix_unstructured_metadata_type', 'unstructured_metadata', ['document_type'], unique=False)
    op.create_table('node_relationships',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('doc_id', sa.Uuid(), nullable=False),
    sa.Column('source_node_id', sa.Uuid(), nullable=False),
    sa.Column('target_node_id', sa.Uuid(), nullable=False),
    sa.Column('relationship', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("relationship IN ('PREVIOUS','NEXT')", name=op.f('ck_node_relationships_relationship')),
    sa.CheckConstraint('source_node_id <> target_node_id', name=op.f('ck_node_relationships_not_self')),
    sa.ForeignKeyConstraint(['doc_id'], ['unstructured_data.id'], name=op.f('fk_node_relationships_doc_id_unstructured_data'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_node_id'], ['text_nodes.id'], name=op.f('fk_node_relationships_source_node_id_text_nodes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_node_id'], ['text_nodes.id'], name=op.f('fk_node_relationships_target_node_id_text_nodes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_node_relationships')),
    sa.UniqueConstraint('source_node_id', 'relationship', name='uq_node_relationships_source_relationship')
    )
    op.create_index('ix_node_relationships_doc', 'node_relationships', ['doc_id'], unique=False)
    # Same updated_at trigger as the 0001 tables (function rag_touch_updated_at already exists).
    for table in TOUCHED:
        op.execute(f'CREATE TRIGGER touch_updated_at BEFORE UPDATE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION rag_touch_updated_at()')


def downgrade():
    op.drop_index('ix_node_relationships_doc', table_name='node_relationships')
    op.drop_table('node_relationships')
    op.drop_index('ix_unstructured_metadata_type', table_name='unstructured_metadata')
    op.drop_index('ix_unstructured_metadata_thread', table_name='unstructured_metadata')
    op.drop_index('ix_unstructured_metadata_filters', table_name='unstructured_metadata')
    op.drop_table('unstructured_metadata')
    op.drop_index('ix_text_nodes_tsv', table_name='text_nodes', postgresql_using='gin')
    op.drop_index('ix_text_nodes_thread', table_name='text_nodes')
    op.drop_index('ix_text_nodes_table', table_name='text_nodes', postgresql_where=sa.text('table_id IS NOT NULL'))
    op.drop_index('ix_text_nodes_embedding', table_name='text_nodes', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_table('text_nodes')
    op.drop_index('ix_image_rag_vector_table_thread', table_name='image_rag_vector_table')
    op.drop_index('ix_image_rag_vector_table_embedding', table_name='image_rag_vector_table', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_table('image_rag_vector_table')
    op.drop_index('ix_unstructured_data_standardization_waiting', table_name='unstructured_data', postgresql_where=sa.text("standardization_status = 'WAITING'"))
    op.drop_index('ix_unstructured_data_standardization_running', table_name='unstructured_data', postgresql_where=sa.text("standardization_status = 'RUNNING'"))
    op.drop_index('ix_unstructured_data_parquet_waiting', table_name='unstructured_data', postgresql_where=sa.text("parquet_status = 'WAITING'"))
    op.drop_index('ix_unstructured_data_parquet_running', table_name='unstructured_data', postgresql_where=sa.text("parquet_status = 'RUNNING'"))
    op.drop_index('ix_unstructured_data_metaclassifier_waiting', table_name='unstructured_data', postgresql_where=sa.text("metaclassifier_status = 'WAITING'"))
    op.drop_index('ix_unstructured_data_metaclassifier_running', table_name='unstructured_data', postgresql_where=sa.text("metaclassifier_status = 'RUNNING'"))
    op.drop_index('ix_unstructured_data_markdown_waiting', table_name='unstructured_data', postgresql_where=sa.text("markdown_status = 'WAITING'"))
    op.drop_index('ix_unstructured_data_markdown_running', table_name='unstructured_data', postgresql_where=sa.text("markdown_status = 'RUNNING'"))
    op.drop_index('ix_unstructured_data_image_waiting', table_name='unstructured_data', postgresql_where=sa.text("image_status = 'WAITING'"))
    op.drop_index('ix_unstructured_data_image_running', table_name='unstructured_data', postgresql_where=sa.text("image_status = 'RUNNING'"))
    op.drop_table('unstructured_data')
    op.drop_table('fabric_unstructured_thread')
    op.drop_index('ix_unstructured_thread_created_by', table_name='unstructured_thread')
    op.drop_table('unstructured_thread')
    op.drop_table('data_fabric')
