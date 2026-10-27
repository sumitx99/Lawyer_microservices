import os
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError, DBAPIError

from datetime import date, datetime, timedelta, timezone

from pymicroservices.app.models import (Chunk, DataFabric, Document, FabricUnstructuredThread, ImageRagVector,
                                       NodeRelationship, OutboxEvent, ProcessedEvent, Section, TextNode,
                                       UnstructuredData, UnstructuredMetadata, UnstructuredThread)
from pymicroservices.app.utilities.events import RagEvent
from pymicroservices.app.utilities.postgresql_helper import Database

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get('RAG_RUN_INTEGRATION') != '1',
                                 reason='Set RAG_RUN_INTEGRATION=1 for real database tests.')]


class RollbackFixture(Exception):
    pass


async def test_schema_search_constraints_timestamps_and_atomic_rollback():
    db = Database()
    project, doc_id, section_id, chunk_id = (uuid4() for _ in range(4))
    event = RagEvent(event_type='doc.uploaded', project_id=project, doc_id=doc_id)
    try:
        with pytest.raises(RollbackFixture):
            async with db.transaction() as session:
                await session.execute(insert(Document).values(
                    id=doc_id, project_id=project, filename='test.pdf', media_type='application/pdf',
                    byte_count=25, sha256='a' * 64, object_key=f'integration/{doc_id}/test.pdf'))
                await session.execute(insert(OutboxEvent).values(
                    id=event.event_id, project_id=project, event_type=event.event_type,
                    aggregate_id=doc_id, payload=event.model_dump(mode='json')))
                await session.execute(insert(Section).values(
                    id=section_id, doc_id=doc_id, project_id=project, ordinal=0,
                    content='Evidence section', page_start=1, page_end=2))
                await session.execute(insert(Chunk).values(
                    id=chunk_id, doc_id=doc_id, project_id=project, section_id=section_id,
                    ordinal=0, content='Medical evidence from the document', token_count=6,
                    embedding=[0.1] * 1024, embedding_status='READY', page_start=1, page_end=1))
                assert await session.scalar(text('SELECT vector_dims(embedding) FROM chunks WHERE id=:id'),
                                            {'id': chunk_id}) == 1024
                assert await session.scalar(text("SELECT search_vector @@ plainto_tsquery('english', 'evidence') FROM chunks WHERE id=:id"), {'id': chunk_id})
                before = await session.scalar(select(Document.updated_at).where(Document.id == doc_id))
                await session.execute(text("UPDATE documents SET status='PARSING', attempt_count=attempt_count+1 WHERE id=:id"), {'id': doc_id})
                after = await session.scalar(select(Document.updated_at).where(Document.id == doc_id))
                assert after > before
                # A project cannot attach its section to another project's document.
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(insert(Section).values(
                            id=uuid4(), doc_id=doc_id, project_id=uuid4(), ordinal=1, content='wrong project'))
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(text("UPDATE documents SET status='UNKNOWN' WHERE id=:id"), {'id': doc_id})
                # Vector width and embedding completeness are database invariants.
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(text("UPDATE chunks SET embedding='[1,2,3]'::vector WHERE id=:id"), {'id': chunk_id})
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(text('UPDATE chunks SET embedding=NULL WHERE id=:id'), {'id': chunk_id})
                await session.execute(insert(ProcessedEvent).values(
                    event_id=event.event_id, consumer_name='test', project_id=project))
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(insert(ProcessedEvent).values(
                            event_id=event.event_id, consumer_name='test', project_id=project))
                raise RollbackFixture()
        async with db.transaction() as session:
            assert await session.scalar(select(Document.id).where(Document.id == doc_id)) is None
            assert await session.scalar(select(OutboxEvent.id).where(OutboxEvent.id == event.event_id)) is None
            assert await session.scalar(select(Chunk.id).where(Chunk.id == chunk_id)) is None
    finally:
        await db.close()


async def rejects(session, statement, rule, error=IntegrityError, **params):
    """Run one statement in a savepoint and require the database to refuse it because of `rule`."""
    with pytest.raises(error, match=rule):
        async with session.begin_nested():
            await session.execute(text(statement) if isinstance(statement, str) else statement, params)


async def test_folder_pipeline_schema_rules_search_and_cascades():
    db = Database()
    fabric, thread, other_thread, doc, node_a, node_b = (uuid4() for _ in range(6))
    file = dict(thread_id=thread, file_name='lease.pdf', file_type='pdf', category='document',
                byte_count=1024, sha256='b' * 64)
    try:
        with pytest.raises(RollbackFixture):
            async with db.transaction() as session:
                await session.execute(insert(DataFabric).values(id=fabric, name='Client matter', created_by='user-1'))
                for folder in (thread, other_thread):
                    await session.execute(insert(UnstructuredThread).values(id=folder, name='Folder', created_by='user-1'))
                await session.execute(insert(FabricUnstructuredThread).values(fabric_id=fabric, thread_id=thread))
                await session.execute(insert(UnstructuredData).values(id=doc, s3_path=f'THREAD_{thread}/pdf/doc_{doc}/lease.pdf', **file))

                # Files: one copy per folder (by SHA-256); the same file may sit in another folder.
                await rejects(session, insert(UnstructuredData).values(id=uuid4(), s3_path='dup', **file), 'uq_unstructured_data_thread_sha256')
                await session.execute(insert(UnstructuredData).values(
                    id=uuid4(), s3_path='other-folder-copy', **(file | {'thread_id': other_thread})))
                await rejects(session, "UPDATE unstructured_data SET file_type='exe' WHERE id=:doc", 'ck_unstructured_data_file_type', doc=doc)
                await rejects(session, "UPDATE unstructured_data SET sha256='not-a-hash' WHERE id=:doc", 'ck_unstructured_data_sha256', doc=doc)

                # Per-stage status: valid values only; RUNNING iff an owner and a lease are present.
                await rejects(session, "UPDATE unstructured_data SET markdown_status='DONE' WHERE id=:doc", 'ck_unstructured_data_markdown_status', doc=doc)
                await rejects(session, "UPDATE unstructured_data SET markdown_status='RUNNING' WHERE id=:doc", 'ck_unstructured_data_markdown_running_owner', doc=doc)
                await rejects(session, "UPDATE unstructured_data SET image_attempts=-1 WHERE id=:doc", 'ck_unstructured_data_image_attempts', doc=doc)
                lease = datetime.now(timezone.utc) + timedelta(minutes=2)
                await session.execute(text("""UPDATE unstructured_data SET standardization_status='RUNNING',
                    standardization_owner='worker-1', standardization_lease_expires_at=:lease,
                    standardization_attempts=1 WHERE id=:doc"""), {'doc': doc, 'lease': lease})
                await rejects(session, "UPDATE unstructured_data SET standardization_status='COMPLETED' WHERE id=:doc",
                              'ck_unstructured_data_standardization_running_owner', doc=doc)
                before = await session.scalar(select(UnstructuredData.updated_at).where(UnstructuredData.id == doc))
                await session.execute(text("""UPDATE unstructured_data SET standardization_status='COMPLETED',
                    standardization_owner=NULL, standardization_lease_expires_at=NULL WHERE id=:doc"""), {'doc': doc})
                assert await session.scalar(select(UnstructuredData.updated_at).where(UnstructuredData.id == doc)) > before

                # Text nodes: language-specific full-text search, 1024-dim vectors, folder consistency.
                node = dict(doc_id=doc, thread_id=thread, total_chunks=2, token_count=8, embedding=[0.1] * 1024)
                await session.execute(insert(TextNode).values(
                    id=node_a, chunk_index=0, ts_config='english', heading_path='Lease > Termination',
                    content='The tenants were evicted after notices were served.', page_start=1, page_end=1, **node))
                await session.execute(insert(TextNode).values(
                    id=node_b, chunk_index=1, node_type='table', table_id='#/tables/0', ts_config='simple',
                    content='किराया हर महीने देय है', page_start=2, page_end=2, **node))
                match = "SELECT count(*) FROM text_nodes WHERE doc_id=:doc AND tsv @@ plainto_tsquery(CAST(:cfg AS regconfig), :q)"
                assert await session.scalar(text(match), {'doc': doc, 'cfg': 'english', 'q': 'tenant evict notice'}) == 1
                assert await session.scalar(text(match), {'doc': doc, 'cfg': 'simple', 'q': 'किराया'}) == 1
                await rejects(session, insert(TextNode).values(id=uuid4(), chunk_index=2, content='x',
                                                               **(node | {'thread_id': other_thread, 'total_chunks': 3})),
                              'fk_text_nodes_doc_id_unstructured_data')  # a chunk cannot move to another folder
                await rejects(session, insert(TextNode).values(id=uuid4(), chunk_index=5, content='x', **node),
                              'ck_text_nodes_counts')
                await rejects(session, "UPDATE text_nodes SET embedding='[1,2,3]'::vector WHERE id=:id",
                              'expected 1024 dimensions', DBAPIError, id=node_a)
                await rejects(session, 'UPDATE text_nodes SET embedding=NULL WHERE id=:id', 'embedding', id=node_a)

                # Neighbour links: one PREVIOUS/NEXT per node, never to itself.
                await session.execute(insert(NodeRelationship).values(
                    doc_id=doc, source_node_id=node_a, target_node_id=node_b, relationship='NEXT'))
                await session.execute(insert(NodeRelationship).values(
                    doc_id=doc, source_node_id=node_b, target_node_id=node_a, relationship='PREVIOUS'))
                await rejects(session, insert(NodeRelationship).values(
                    doc_id=doc, source_node_id=node_a, target_node_id=node_b, relationship='NEXT'),
                    'uq_node_relationships_source_relationship')
                await rejects(session, insert(NodeRelationship).values(
                    doc_id=doc, source_node_id=node_a, target_node_id=node_a, relationship='PREVIOUS'),
                    'ck_node_relationships_not_self')

                # Images: logos are never stored; metadata keeps typed legal fields.
                image = dict(doc_id=doc, thread_id=thread, s3_path='img.png', page_no=3,
                             description='Bar chart of monthly rent', embedding=[0.2] * 1024)
                await session.execute(insert(ImageRagVector).values(image_index=0, image_type='graph', **image))
                await rejects(session, insert(ImageRagVector).values(image_index=1, image_type='logo', **image),
                              'ck_image_rag_vector_table_image_type')
                await session.execute(insert(UnstructuredMetadata).values(
                    doc_id=doc, thread_id=thread, document_type='lease agreement', jurisdiction='IN',
                    decision_date=date(2026, 5, 1), parties=[{'name': 'A. Sharma', 'role': 'tenant'}],
                    evidence={'parties': [{'node_id': str(node_a), 'page': 1}]}, title_embedding=[0.3] * 1024))

                # Deleting the folder removes every file, chunk, link, image, metadata row and project link.
                await session.execute(text('DELETE FROM unstructured_thread WHERE id=:thread'), {'thread': thread})
                for table in ['unstructured_data', 'text_nodes', 'node_relationships', 'image_rag_vector_table',
                              'unstructured_metadata', 'fabric_unstructured_thread']:
                    column = 'thread_id' if table not in {'node_relationships'} else 'doc_id'
                    value = doc if column == 'doc_id' else thread
                    assert await session.scalar(text(f'SELECT count(*) FROM {table} WHERE {column}=:v'), {'v': value}) == 0, table
                raise RollbackFixture()
        async with db.transaction() as session:
            for model, key in [(DataFabric.id, fabric), (UnstructuredThread.id, other_thread)]:
                assert await session.scalar(select(model).where(model == key)) is None
    finally:
        await db.close()
