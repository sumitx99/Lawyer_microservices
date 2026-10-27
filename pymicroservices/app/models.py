"""RAG persistence schema. Migrations are owned by this backend, not consumers.

Two generations live side by side until the folder pipeline replaces the per-document one:
Document/Section/Chunk (current pipeline) and the design-doc tables from DataFabric down
(folders, one row per file with per-stage status, text_nodes, images, metadata).
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (BigInteger, CheckConstraint, Computed, Date, DateTime, ForeignKey, ForeignKeyConstraint,
                        Index, Integer, MetaData, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, REGCONFIG, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        'ix': 'ix_%(table_name)s_%(column_0_name)s', 'uq': 'uq_%(table_name)s_%(column_0_name)s',
        'ck': 'ck_%(table_name)s_%(constraint_name)s',
        'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
        'pk': 'pk_%(table_name)s',
    })


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Document(Timestamped, Base):
    __tablename__ = 'documents'
    __table_args__ = (
        UniqueConstraint('id', 'project_id'),
        UniqueConstraint('project_id', 'upload_idempotency_key', name='uq_documents_project_upload_key'),
        CheckConstraint("status IN ('PENDING','PARSING','PARSED','INDEXING','INDEXED','FAILED')", name='status'),
        CheckConstraint('byte_count >= 0 AND attempt_count >= 0', name='nonnegative_counts'),
        CheckConstraint('index_attempt_count >= 0', name='index_attempt_count'),
        Index('ix_documents_project_created', 'project_id', 'created_at'),
        Index('ix_documents_recovery', 'status', 'lease_expires_at'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID]
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(128))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    parsed_object_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default='PENDING')
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    index_version: Mapped[int] = mapped_column(Integer, server_default='0')
    upload_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    parse_summary: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    index_attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    index_summary: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Section(Timestamped, Base):
    __tablename__ = 'sections'
    __table_args__ = (
        ForeignKeyConstraint(['doc_id', 'project_id'], ['documents.id', 'documents.project_id'], ondelete='CASCADE'),
        UniqueConstraint('id', 'doc_id', 'project_id'), UniqueConstraint('doc_id', 'ordinal'),
        CheckConstraint('ordinal >= 0', name='ordinal'),
        CheckConstraint('page_start IS NULL OR page_start >= 1', name='page_start'),
        CheckConstraint('page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)', name='page_end'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID]
    doc_id: Mapped[UUID]
    ordinal: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    content: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)


class Chunk(Timestamped, Base):
    __tablename__ = 'chunks'
    __table_args__ = (
        ForeignKeyConstraint(['section_id', 'doc_id', 'project_id'], ['sections.id', 'sections.doc_id', 'sections.project_id'], ondelete='CASCADE'),
        UniqueConstraint('doc_id', 'ordinal'),
        CheckConstraint("chunk_type IN ('text','table')", name='chunk_type'),
        CheckConstraint("embedding_status IN ('PENDING','READY','FAILED')", name='embedding_status'),
        CheckConstraint("(embedding_status = 'READY') = (embedding IS NOT NULL)", name='embedding_consistency'),
        CheckConstraint('ordinal >= 0 AND token_count >= 0', name='nonnegative_counts'),
        CheckConstraint('page_start IS NULL OR page_start >= 1', name='page_start'),
        CheckConstraint('page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)', name='page_end'),
        Index('ix_chunks_project_doc', 'project_id', 'doc_id'),
        Index('ix_chunks_search', 'search_vector', postgresql_using='gin'),
        Index('ix_chunks_embedding', 'embedding', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'}),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID]
    doc_id: Mapped[UUID]
    section_id: Mapped[UUID]
    ordinal: Mapped[int] = mapped_column(Integer)
    chunk_type: Mapped[str] = mapped_column(String(16), server_default='text')
    heading_path: Mapped[str] = mapped_column(Text, server_default='')
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    table_id: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    embedding: Mapped[list | None] = mapped_column(Vector(1024))
    embedding_status: Mapped[str] = mapped_column(String(16), server_default='PENDING')
    embedding_model: Mapped[str] = mapped_column(Text, server_default='BAAI/bge-m3')
    search_vector: Mapped[str] = mapped_column(TSVECTOR, Computed(
        "to_tsvector('english'::regconfig, coalesce(heading_path, '') || ' ' || content)", persisted=True))


class QueryJob(Timestamped, Base):
    __tablename__ = 'query_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','COMPLETED','FAILED')", name='status'),
        CheckConstraint('attempt_count >= 0', name='attempt_count'),
        Index('ix_query_jobs_project_created', 'project_id', 'created_at'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID]
    query: Mapped[str] = mapped_column(Text)
    doc_ids: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default='PENDING')
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    error_message: Mapped[str | None] = mapped_column(Text)


class OutboxEvent(Timestamped, Base):
    __tablename__ = 'outbox_events'
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','PUBLISHED','FAILED')", name='status'),
        CheckConstraint('attempt_count >= 0', name='attempt_count'),
        Index('ix_outbox_dispatch', 'status', 'available_at'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default='PENDING')
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class ProcessedEvent(Base):
    __tablename__ = 'processed_events'
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    consumer_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[UUID]
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------------------------
# Folder pipeline (design doc): project -> folder -> file, with per-stage status on each file.
# ---------------------------------------------------------------------------------------------

STAGES = ('standardization', 'markdown', 'image', 'metaclassifier', 'parquet')
STAGE_STATUSES = ('WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'INAPPLICABLE')
FILE_TYPES = ('pdf', 'docx', 'doc', 'txt', 'pptx', 'xlsx', 'csv', 'jpg', 'png')
CATEGORIES = ('document', 'presentation', 'spreadsheet', 'csv', 'image', 'text')
EMPTY_ARRAY, EMPTY_OBJECT = text("'[]'::jsonb"), text("'{}'::jsonb")


def _quoted(values):
    return ', '.join(f"'{value}'" for value in values)


def _stage_columns():
    """status / updated_at / attempts / error, plus owner + lease for heartbeat and fencing."""
    columns = {}
    for stage in STAGES:
        columns[f'{stage}_status'] = mapped_column(String(16), nullable=True)
        columns[f'{stage}_updated_at'] = mapped_column(DateTime(timezone=True), nullable=True)
        columns[f'{stage}_attempts'] = mapped_column(Integer, nullable=False, server_default='0')
        columns[f'{stage}_error'] = mapped_column(Text, nullable=True)
        columns[f'{stage}_owner'] = mapped_column(String(128), nullable=True)
        columns[f'{stage}_lease_expires_at'] = mapped_column(DateTime(timezone=True), nullable=True)
    return columns


def _stage_table_args():
    args = []
    for stage in STAGES:
        args += [
            CheckConstraint(f'{stage}_status IN ({_quoted(STAGE_STATUSES)})', name=f'{stage}_status'),
            CheckConstraint(f'{stage}_attempts >= 0', name=f'{stage}_attempts'),
            # A RUNNING stage always has an owner and a lease; nothing else may keep them.
            CheckConstraint(f"coalesce({stage}_status = 'RUNNING', false) = "
                            f"({stage}_owner IS NOT NULL AND {stage}_lease_expires_at IS NOT NULL)",
                            name=f'{stage}_running_owner'),
            Index(f'ix_unstructured_data_{stage}_waiting', f'{stage}_updated_at',
                  postgresql_where=text(f"{stage}_status = 'WAITING'")),
            Index(f'ix_unstructured_data_{stage}_running', f'{stage}_lease_expires_at',
                  postgresql_where=text(f"{stage}_status = 'RUNNING'")),
        ]
    return args


StageColumns = type('StageColumns', (), _stage_columns())


class DataFabric(Timestamped, Base):
    """A project (client or matter) that groups folders."""
    __tablename__ = 'data_fabric'
    __table_args__ = (Index('ix_data_fabric_created_by', 'created_by', 'created_at'),)
    description: Mapped[str | None] = mapped_column(Text)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))


class UnstructuredThread(Timestamped, Base):
    """A folder of uploaded files; status is derived from its files."""
    __tablename__ = 'unstructured_thread'
    __table_args__ = (
        CheckConstraint("status IN ('UPLOADING','PROCESSING','COMPLETED','PARTIAL','FAILED')", name='status'),
        Index('ix_unstructured_thread_created_by', 'created_by', 'created_at'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default='UPLOADING')
    tags: Mapped[list] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    jurisdiction: Mapped[str | None] = mapped_column(Text)


class FabricUnstructuredThread(Base):
    __tablename__ = 'fabric_unstructured_thread'
    fabric_id: Mapped[UUID] = mapped_column(ForeignKey('data_fabric.id', ondelete='CASCADE'), primary_key=True)
    thread_id: Mapped[UUID] = mapped_column(ForeignKey('unstructured_thread.id', ondelete='CASCADE'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UnstructuredData(StageColumns, Timestamped, Base):
    """One uploaded file, with upload status and one status/attempt/error/lease set per stage."""
    __tablename__ = 'unstructured_data'
    __table_args__ = (
        UniqueConstraint('thread_id', 'sha256', name='uq_unstructured_data_thread_sha256'),
        UniqueConstraint('id', 'thread_id', name='uq_unstructured_data_id_thread'),
        CheckConstraint(f'file_type IN ({_quoted(FILE_TYPES)})', name='file_type'),
        CheckConstraint(f'category IN ({_quoted(CATEGORIES)})', name='category'),
        CheckConstraint("upload_status IN ('RUNNING','COMPLETED','FAILED')", name='upload_status'),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='sha256'),
        CheckConstraint('byte_count >= 0 AND (page_count IS NULL OR page_count >= 0)', name='nonnegative'),
        *_stage_table_args(),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    thread_id: Mapped[UUID] = mapped_column(ForeignKey('unstructured_thread.id', ondelete='CASCADE'))
    file_name: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    s3_path: Mapped[str] = mapped_column(Text, unique=True)
    upload_status: Mapped[str] = mapped_column(String(16), server_default='RUNNING')
    upload_error: Mapped[str | None] = mapped_column(Text)
    s3_path_tomarkdown: Mapped[str | None] = mapped_column(Text)
    s3_path_images: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(32))
    processing_summary: Mapped[dict] = mapped_column(JSONB, server_default=EMPTY_OBJECT)


class TextNode(Timestamped, Base):
    """A text or table chunk with its BGE-M3 vector and per-language full-text vector."""
    __tablename__ = 'text_nodes'
    __table_args__ = (
        ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'],
                             ondelete='CASCADE'),
        UniqueConstraint('doc_id', 'chunk_index', name='uq_text_nodes_doc_chunk'),
        CheckConstraint("node_type IN ('text','table')", name='node_type'),
        CheckConstraint('chunk_index >= 0 AND total_chunks > chunk_index AND token_count >= 0', name='counts'),
        CheckConstraint('page_start IS NULL OR page_start >= 1', name='page_start'),
        CheckConstraint('page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)', name='page_end'),
        Index('ix_text_nodes_thread', 'thread_id'),
        Index('ix_text_nodes_table', 'doc_id', 'table_id', postgresql_where=text('table_id IS NOT NULL')),
        Index('ix_text_nodes_tsv', 'tsv', postgresql_using='gin'),
        Index('ix_text_nodes_embedding', 'embedding', postgresql_using='hnsw',
              postgresql_ops={'embedding': 'vector_cosine_ops'}),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    doc_id: Mapped[UUID]
    thread_id: Mapped[UUID]
    chunk_index: Mapped[int] = mapped_column(Integer)
    total_chunks: Mapped[int] = mapped_column(Integer)
    node_type: Mapped[str] = mapped_column(String(16), server_default='text')
    heading_path: Mapped[str] = mapped_column(Text, server_default='')
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    table_id: Mapped[str | None] = mapped_column(Text)
    node_metadata: Mapped[dict] = mapped_column('metadata', JSONB, server_default=EMPTY_OBJECT)
    ts_config: Mapped[str] = mapped_column(REGCONFIG, server_default=text("'simple'::regconfig"))
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(
        "to_tsvector(ts_config, coalesce(heading_path, '') || ' ' || content)", persisted=True))
    embedding: Mapped[list] = mapped_column(Vector(1024))
    embedding_model: Mapped[str] = mapped_column(Text, server_default='BAAI/bge-m3')


class NodeRelationship(Base):
    """PREVIOUS/NEXT links between a document's chunks, for neighbour expansion."""
    __tablename__ = 'node_relationships'
    __table_args__ = (
        UniqueConstraint('source_node_id', 'relationship', name='uq_node_relationships_source_relationship'),
        CheckConstraint("relationship IN ('PREVIOUS','NEXT')", name='relationship'),
        CheckConstraint('source_node_id <> target_node_id', name='not_self'),
        Index('ix_node_relationships_doc', 'doc_id'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    doc_id: Mapped[UUID] = mapped_column(ForeignKey('unstructured_data.id', ondelete='CASCADE'))
    source_node_id: Mapped[UUID] = mapped_column(ForeignKey('text_nodes.id', ondelete='CASCADE'))
    target_node_id: Mapped[UUID] = mapped_column(ForeignKey('text_nodes.id', ondelete='CASCADE'))
    relationship: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImageRagVector(Timestamped, Base):
    """Image RAG output: type, model-written description and its embedding (logos are not stored)."""
    __tablename__ = 'image_rag_vector_table'
    __table_args__ = (
        ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'],
                             ondelete='CASCADE'),
        UniqueConstraint('doc_id', 'image_index', name='uq_image_rag_vector_table_doc_image'),
        CheckConstraint("image_type IN ('table','graph','image')", name='image_type'),
        CheckConstraint('image_index >= 0 AND (page_no IS NULL OR page_no >= 1)', name='positions'),
        Index('ix_image_rag_vector_table_thread', 'thread_id'),
        Index('ix_image_rag_vector_table_tsv', 'tsv', postgresql_using='gin'),
        Index('ix_image_rag_vector_table_embedding', 'embedding', postgresql_using='hnsw',
              postgresql_ops={'embedding': 'vector_cosine_ops'}),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    doc_id: Mapped[UUID]
    thread_id: Mapped[UUID]
    image_index: Mapped[int] = mapped_column(Integer)
    s3_path: Mapped[str] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer)
    image_type: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('simple'::regconfig, description)", persisted=True))
    image_metadata: Mapped[dict] = mapped_column('metadata', JSONB, server_default=EMPTY_OBJECT)
    embedding: Mapped[list] = mapped_column(Vector(1024))
    embedding_model: Mapped[str] = mapped_column(Text, server_default='BAAI/bge-m3')


class UnstructuredMetadata(Timestamped, Base):
    """Metaclassifier output. evidence maps each field to the nodes/pages that support it."""
    __tablename__ = 'unstructured_metadata'
    __table_args__ = (
        ForeignKeyConstraint(['doc_id', 'thread_id'], ['unstructured_data.id', 'unstructured_data.thread_id'],
                             ondelete='CASCADE'),
        Index('ix_unstructured_metadata_thread', 'thread_id'),
        Index('ix_unstructured_metadata_filters', 'jurisdiction', 'court', 'decision_date'),
        Index('ix_unstructured_metadata_type', 'document_type'),
    )
    doc_id: Mapped[UUID] = mapped_column(primary_key=True)
    thread_id: Mapped[UUID]
    title: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str | None] = mapped_column(Text)
    jurisdiction: Mapped[str | None] = mapped_column(Text)
    court: Mapped[str | None] = mapped_column(Text)
    case_number: Mapped[str | None] = mapped_column(Text)
    parties: Mapped[list] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    judges: Mapped[list] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    decision_date: Mapped[date | None] = mapped_column(Date)
    citation: Mapped[str | None] = mapped_column(Text)
    statutes_cited: Mapped[list] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    classification: Mapped[list] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    language: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    # Deed/contract facts with no column of their own: dates, amounts, property, governing law.
    details: Mapped[dict] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    model: Mapped[str | None] = mapped_column(Text)
    title_embedding: Mapped[list | None] = mapped_column(Vector(1024))
