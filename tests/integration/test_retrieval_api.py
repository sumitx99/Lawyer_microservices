"""Retrieval with real Postgres and injectable deterministic models."""
from datetime import date
import os
from types import SimpleNamespace
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, func, select, text

from pymicroservices.app.main import create_app
from pymicroservices.app.models import (DataFabric, FabricUnstructuredThread, ImageRagVector, TextNode,
    UnstructuredData, UnstructuredMetadata, UnstructuredThread)
from pymicroservices.app.utilities.retrieval_models import ModelUnavailable
from pymicroservices.app.utilities.secrets_control import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.environ.get('RAG_RUN_INTEGRATION') != '1',
                                                         reason='Requires local Postgres.')]


def vector(axis=0):
    return [1. if n == axis else 0. for n in range(1024)]


class Embedder:
    def __init__(self): self.calls = []
    async def embed_async(self, texts):
        self.calls.extend(texts)
        return [vector() for _ in texts]


class Reranker:
    def __init__(self): self.calls = []
    async def score_async(self, query, passages):
        self.calls.append((query, passages))
        return [10. if query.lower() in passage.lower() else 0. for passage in passages]


@pytest.fixture
async def corpus():
    settings = get_settings().model_copy(update={'outbox_enabled': False, 'upload_cleanup_enabled': False})
    app = create_app(settings)
    owner, other = 'retrieval-test-' + uuid4().hex, 'retrieval-test-' + uuid4().hex
    project, outside, foreign = [uuid4() for _ in range(3)]
    threads = [uuid4() for _ in range(4)]
    docs = [uuid4() for _ in range(7)]
    nodes, images = [uuid4() for _ in range(10)], [uuid4() for _ in range(2)]
    async with app.router.lifespan_context(app):
        app.state.query_embedder, app.state.reranker = Embedder(), Reranker()
        try:
            async with app.state.db.transaction() as session:
                session.add_all([DataFabric(id=id, name='Retrieval fixture', created_by=user)
                    for id, user in [(project, owner), (outside, owner), (foreign, other)]])
                session.add_all([UnstructuredThread(id=id, name='Evidence', created_by=owner if n < 3 else other)
                    for n, id in enumerate(threads)])
                await session.flush()
                session.add_all([FabricUnstructuredThread(fabric_id=p, thread_id=t) for p, t in [
                    (project, threads[0]), (project, threads[1]), (outside, threads[2]),
                    (foreign, threads[3]), (project, threads[3])]])
                for n, id in enumerate(docs):
                    thread = threads[0] if n in (0, 3, 4) else threads[1] if n in (1, 2) else threads[n - 3]
                    session.add(UnstructuredData(id=id, thread_id=thread, file_name=f'fixture-{n}.pdf', file_type='pdf',
                        category='document', byte_count=10, sha256=str(n) * 64, s3_path=f'retrieval-test/{id}',
                        upload_status='COMPLETED', markdown_status='WAITING' if n in (2, 3) else 'COMPLETED',
                        image_status='COMPLETED' if n == 2 else 'INAPPLICABLE'))
                await session.flush()
                content = [
                    ('The termination clause requires one month notice.', 0, ['Operative', '3'], 'english', 'text'),
                    ('Section 138 of the Negotiable Instruments Act applies to this complaint.', 0, ['Operative', '138'], 'english', 'text'),
                    ('The tenant shall pay monthly rent of Rs. 15000.', 0, ['Operative', '2', '2.1'], 'english', 'text'),
                    ('The security deposit is Rs. 45000.', 0, ['Operative', '2', '2.2'], 'english', 'table'),
                    ('मैं शपथ लेता हूँ कि मेरा निवास पटना है। धारा 138 का उल्लेख है।', 1, ['Operative', '1'], 'simple', 'text'),
                    ('मैंने यह शपथपत्र स्वेच्छा से प्रस्तुत किया है।', 1, ['Operative', '2'], 'simple', 'text'),
                    ('Incomplete secret unicorn document.', 3, [], 'english', 'text'),
                    ('Vector-only azimuth signal.', 4, [], 'english', 'text'),
                    ('Same user other project confidential evidence.', 5, [], 'english', 'text'),
                    ('Foreign owner confidential evidence.', 6, [], 'english', 'text')]
                counts = {0: 4, 1: 2, 3: 1, 4: 1, 5: 1, 6: 1}
                indices = {}
                for n, (body, file, path, config, kind) in enumerate(content):
                    index = indices.get(file, 0)
                    indices[file] = index + 1
                    session.add(TextNode(id=nodes[n], doc_id=docs[file], thread_id=threads[0] if file in (0, 3, 4)
                        else threads[1] if file == 1 else threads[file - 3], chunk_index=index,
                        total_chunks=counts[file], node_type=kind, heading_path='LEASE' if file == 0 else '',
                        content=body, token_count=20, page_start=index + 1, page_end=index + 1,
                        node_metadata={'legal_path': path}, ts_config=config, embedding=vector(0 if n == 7 else 1)))
                for n, id in enumerate(images):
                    session.add(ImageRagVector(id=id, doc_id=docs[2], thread_id=threads[1], image_index=n,
                        s3_path=f'retrieval-test/image-{id}', page_no=n+5, image_type='graph' if n == 0 else 'image',
                        description='Rent chart of monthly payments' if n == 0 else 'A courtroom photograph',
                        image_metadata={'model_derived': True}, embedding=vector(2)))
                for n in (0, 1, 2, 3):
                    session.add(UnstructuredMetadata(doc_id=docs[n], thread_id=threads[0] if n in (0, 3) else threads[1],
                        title='Lease agreement' if n == 0 else 'Hindi affidavit', document_type='lease_deed' if n == 0 else 'affidavit',
                        jurisdiction='Patna', court='Magistrate', case_number='1234 of 2025' if n == 0 else None,
                        statutes_cited=['Section 138'] if n == 0 else [], decision_date=date(2026, 4, 1),
                        evidence={'title': [{'kind': 'text', 'id': str(nodes[2]), 'page': 999, 'index': 999},
                            {'kind': 'image', 'id': str(images[0]), 'page': 999, 'index': 999},
                            {'kind': 'text', 'id': str(nodes[8]), 'page': 999}]}
                            if n == 0 else {'summary': [{'kind': 'image', 'id': str(images[0]), 'page': 999}]}))
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test', headers={'X-User-Id': owner}) as client:
                yield SimpleNamespace(app=app, client=client, owner=owner, other=other, project=project, outside=outside,
                                      foreign=foreign, threads=threads, docs=docs, nodes=nodes, images=images)
        finally:
            async with app.state.db.transaction() as session:
                await session.execute(delete(DataFabric).where(DataFabric.created_by.in_([owner, other])))
                await session.execute(delete(UnstructuredThread).where(UnstructuredThread.created_by.in_([owner, other])))
            async with app.state.db.transaction() as session:
                assert not await session.scalar(select(func.count()).select_from(UnstructuredData).where(UnstructuredData.id.in_(docs)))
                assert not await session.scalar(select(func.count()).select_from(TextNode).where(TextNode.id.in_(nodes)))


async def call(corpus, endpoint, code=200, **payload):
    response = await corpus.client.post('/retrieval/' + endpoint, json={'project_id': str(corpus.project), **payload})
    assert response.status_code == code, response.text
    return response.json()


async def test_vector_keyword_hindi_reference_and_images(corpus):
    corpus.app.state.retrieval.settings = corpus.app.state.settings.model_copy(update={'reranker_enabled': False})
    result = await call(corpus, 'search', query='unmatched gibberish', top_k=50)
    assert result['results'][0]['id'] == str(corpus.nodes[7])
    assert result['reranked'] is False and result['timing_ms']['rerank'] == 0
    for query, id in [('terminating', corpus.nodes[0]), ('शपथपत्र', corpus.nodes[5]),
                      ('Section 138', corpus.nodes[1]), ('rent chart', corpus.images[0])]:
        result = await call(corpus, 'search', query=query, top_k=50, max_per_document=50)
        by_id = {r['id']: r for r in result['results']}
        assert by_id[str(id)]['scores']['keyword_rank'] is not None
        if query == 'Section 138':
            assert result['results'][0]['id'] == str(id) and by_id[str(id)]['scores']['reference_match']
    assert str(corpus.nodes[6]) not in by_id
    assert str(corpus.nodes[8]) not in by_id and str(corpus.nodes[9]) not in by_id
    assert all(r['model_derived'] for r in result['results'] if r['kind'] == 'image')


async def test_filters_diversity_and_reranking(corpus):
    result = await call(corpus, 'search', query='security deposit', top_k=50, max_per_document=1)
    assert result['reranked'] and result['results'][0]['id'] == str(corpus.nodes[3])
    ids = [r['doc_id'] for r in result['results']]
    assert len(ids) == len(set(ids))
    for filters in [ {'document_types': ['lease_deed']}, {'doc_ids': [str(corpus.docs[0])]},
                     {'thread_ids': [str(corpus.threads[0])], 'document_types': ['lease_deed']},
                     {'date_from': '2026-04-01', 'date_to': '2026-04-01', 'court': 'Magistrate',
                      'jurisdiction': 'Patna', 'document_types': ['lease_deed']} ]:
        result = await call(corpus, 'search', query='rent', filters=filters, max_per_document=50, top_k=50)
        assert result['results'] and {r['doc_id'] for r in result['results']} == {str(corpus.docs[0])}
    result = await call(corpus, 'search', query='rent', filters={'include_images': False}, top_k=50, max_per_document=50)
    assert all(r['kind'] == 'text' for r in result['results'])
    assert not (await call(corpus, 'search', query='rent', filters={'date_from': '2027-01-01'}))['results']


async def test_list_facts_and_evidence_resolution(corpus):
    listed = await call(corpus, 'list-documents')
    assert {r['doc_id'] for r in listed['items']} == {str(corpus.docs[n]) for n in (0, 1, 2, 4)}
    assert (await call(corpus, 'list-documents', limit=1, offset=1))['items'] == listed['items'][1:2]
    missing = next(r for r in listed['items'] if r['doc_id'] == str(corpus.docs[4]))
    assert missing['title'] is None and missing['stages']['markdown']['status'] == 'COMPLETED'
    facts = await call(corpus, 'document-facts', doc_id=str(corpus.docs[0]))
    first, wrong_file, foreign = facts['evidence']['title']
    assert first['page'] == 3 and first['chunk_index'] == 2
    assert wrong_file['page'] is None and foreign['page'] is None and foreign['chunk_index'] is None
    facts = await call(corpus, 'document-facts', doc_id=str(corpus.docs[2]))
    evidence = facts['evidence']['summary'][0]
    assert evidence['page'] == 5 and evidence['image_index'] == 0 and evidence['chunk_index'] is None
    assert 'details' in facts and 'title_embedding' in facts
    await call(corpus, 'document-facts', code=404, doc_id=str(corpus.docs[4]))
    await call(corpus, 'document-facts', code=404, doc_id=str(corpus.docs[3]))


async def test_expand_neighbours_clause_prefix_and_no_path(corpus):
    result = await call(corpus, 'expand', node_id=str(corpus.nodes[2]))
    assert [r['id'] for r in result['nodes']] == [str(id) for id in corpus.nodes[1:4]]
    result = await call(corpus, 'expand', node_id=str(corpus.nodes[2]), whole_clause=True)
    assert [r['id'] for r in result['nodes']] == [str(id) for id in corpus.nodes[2:4]]
    assert result['nodes'][0]['scores']['rrf'] is None
    result = await call(corpus, 'expand', node_id=str(corpus.nodes[7]), whole_clause=True)
    assert len(result['nodes']) == 1


async def test_security_precedes_models_and_cross_project_ids_are_404(corpus):
    model = corpus.app.state.query_embedder
    for project_id in (corpus.foreign, uuid4()):
        await call(corpus, 'search', code=404, project_id=str(project_id), query='rent')
    assert model.calls == []
    for id in (corpus.docs[5], corpus.docs[6], uuid4()):
        await call(corpus, 'search', code=404, query='rent', filters={'doc_ids': [str(id)]})
        await call(corpus, 'document-facts', code=404, doc_id=str(id))
    for id in (corpus.nodes[8], corpus.nodes[9], uuid4()):
        await call(corpus, 'expand', code=404, node_id=str(id))
    for id in (corpus.threads[2], corpus.threads[3], uuid4()):
        await call(corpus, 'search', code=404, query='rent', filters={'thread_ids': [str(id)]})
    assert model.calls == []


async def test_missing_models_and_inference_errors(corpus):
    class Missing:
        async def score_async(self, *args): raise ModelUnavailable()
    corpus.app.state.reranker = Missing()
    result = await call(corpus, 'search', query='rent')
    assert not result['reranked'] and result['timing_ms']['rerank'] == 0
    class Broken:
        async def score_async(self, *args): raise RuntimeError('internal detail')
        async def embed_async(self, *args): return [[float('nan')] * 1024]
    corpus.app.state.reranker = Broken()
    await call(corpus, 'search', code=503, query='rent')
    corpus.app.state.query_embedder = Broken()
    await call(corpus, 'search', code=503, query='rent')


async def test_image_tsv_and_hnsw_settings_are_transaction_local(corpus):
    async with corpus.app.state.db.transaction() as session:
        assert await session.scalar(select(ImageRagVector.tsv.op('@@')(
            func.websearch_to_tsquery(text("'simple'::regconfig"), 'rent'))).where(ImageRagVector.id == corpus.images[0]))
        before = await session.scalar(text("SELECT current_setting('hnsw.ef_search')"))
    await call(corpus, 'search', query='rent')
    async with corpus.app.state.db.transaction() as session:
        assert await session.scalar(text("SELECT current_setting('hnsw.ef_search')")) == before


async def test_reference_aliases_do_not_match_longer_numbers(corpus):
    async with corpus.app.state.db.transaction() as session:
        node = await session.get(TextNode, corpus.nodes[7])
        node.content, node.node_metadata = 'Section 1380 only, not the shorter reference.', {'legal_path': ['Section 1380']}
        # A labelled clause path ("Section 138") and Devanagari digits in the text both count as the reference.
        (await session.get(TextNode, corpus.nodes[0])).node_metadata = {'legal_path': ['Section 138', '(a)']}
        (await session.get(TextNode, corpus.nodes[5])).content = 'धारा १३८ के अंतर्गत यह शपथपत्र है।'
    corpus.app.state.retrieval.settings = corpus.app.state.settings.model_copy(update={'reranker_enabled': False})
    for query in ('S. 138', 'धारा 138', 'Section १३८'):
        result = await call(corpus, 'search', query=query, top_k=50, max_per_document=50)
        by_id = {r['id']: r for r in result['results']}
        assert all(by_id[str(corpus.nodes[n])]['scores']['reference_match'] for n in (0, 1, 5))
        assert by_id[str(corpus.nodes[7])]['scores']['reference_match'] is False


async def test_headers_and_full_metadata_vector(corpus):
    for endpoint, payload in [('search', {'query': 'rent'}), ('list-documents', {}),
                             ('document-facts', {'doc_id': str(corpus.docs[0])}),
                             ('expand', {'node_id': str(corpus.nodes[0])})]:
        request = corpus.client.build_request('POST', '/retrieval/' + endpoint,
            json={'project_id': str(corpus.project), **payload})
        del request.headers['X-User-Id']
        assert (await corpus.client.send(request)).status_code == 422
    async with corpus.app.state.db.transaction() as session:
        metadata = await session.get(UnstructuredMetadata, corpus.docs[0])
        metadata.title_embedding = vector()
    facts = await call(corpus, 'document-facts', doc_id=str(corpus.docs[0]))
    assert facts['title_embedding'] == vector()
