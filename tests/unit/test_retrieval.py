"""Deterministic retrieval maths, validation and offline model contracts."""
import asyncio
from contextlib import nullcontext
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from pymicroservices.app.schemas import (RetrievalDocumentsRequest, RetrievalExpandRequest, RetrievalFilters,
                                        RetrievalPassage, RetrievalSearchRequest)
from pymicroservices.app.services.retrieval.retrieval_service import diversify, fuse, passage, references, rerank_text
from pymicroservices.app.utilities.retrieval_models import BGEM3, ModelUnavailable, Reranker, cache_dir
from pymicroservices.app.utilities.secrets_control import Settings


def settings(**values):
    return Settings(_env_file=None, postgres_password='t', valkey_password='t', storage_access_key='t', storage_secret_key='t', **values)


def item(doc=None, content='Evidence', **values):
    return RetrievalPassage(kind='text', id=uuid4(), doc_id=doc or uuid4(), thread_id=uuid4(), file_name='order.pdf', content=content, **values)


def test_rrf_weights_ranks_deduplication_and_stable_order():
    a, b = item(), item()
    result = fuse({'vector': [a, b], 'keyword': [a], 'reference': [b, b]}, has_references=True)
    assert result[0].id == b.id
    assert result[0].scores.rrf == pytest.approx(1 / 62 + 2 / 61)
    assert result[1].scores.rrf == pytest.approx(2 / 61)
    assert result[0].scores.reference_match and result[0].scores.keyword_rank is None
    assert result[1].scores.reference_match is False and result[1].scores.vector_rank == 1
    assert a.scores.rrf is None
    no_refs = fuse({'vector': [a], 'keyword': [], 'reference': []})
    assert no_refs[0].scores.reference_match is None


@pytest.mark.parametrize('query,kind,numbers', [
    ('Section 138', 'section', ('138',)), ('S. 138', 'section', ('138',)), ('धारा 138', 'section', ('138',)),
    ('Article 21', 'article', ('21',)), ('Clause 5.2', 'clause', ('5.2',)),
    ('Order 7 Rule 11', 'order', ('7', '11')), ('1234 of 2025', 'case', ('1234', '2025')),
    ('धारा १३८', 'section', ('138',))])
def test_reference_parser(query, kind, numbers):
    [ref] = references(query)
    assert ref['kind'] == kind and ref['numbers'] == numbers


def test_plain_query_and_duplicate_references():
    assert references('What rent is payable?') == []
    assert len(references('Section 138 and Section 138')) == 1
    assert references('Section 1380')[0]['numbers'] == ('1380',)


def test_diversity_preserves_order_and_can_return_fewer_than_top_k():
    doc = uuid4()
    items = [item(doc) for _ in range(4)] + [item()]
    assert diversify(items, 8, 2) == [items[0], items[1], items[4]]


@pytest.mark.parametrize('model,values', [
    (RetrievalSearchRequest, {'query': ''}), (RetrievalSearchRequest, {'query': '  '}),
    (RetrievalSearchRequest, {'query': 'x' * 2001}), (RetrievalSearchRequest, {'query': 'rent', 'top_k': 0}),
    (RetrievalSearchRequest, {'query': 'rent', 'top_k': 51}),
    (RetrievalSearchRequest, {'query': 'rent', 'max_per_document': 0}),
    (RetrievalSearchRequest, {'query': 'rent', 'max_per_document': 51}),
    (RetrievalSearchRequest, {'query': 'rent', 'extra': True}),
    (RetrievalSearchRequest, {'query': 'rent', 'filters': {'extra': True}}),
    (RetrievalDocumentsRequest, {'limit': 201}), (RetrievalDocumentsRequest, {'offset': -1}),
    (RetrievalExpandRequest, {'node_id': str(uuid4()), 'before': 6}),
    (RetrievalExpandRequest, {'node_id': str(uuid4()), 'after': -1})])
def test_invalid_requests(model, values):
    with pytest.raises(ValidationError):
        model(project_id=uuid4(), **values)


def test_filters_dates_and_defaults():
    with pytest.raises(ValidationError):
        RetrievalFilters(date_from='2026-02-01', date_to='2026-01-01')
    request = RetrievalSearchRequest(project_id=uuid4(), query=' rent ')
    assert request.query == 'rent' and request.top_k == 8 and request.max_per_document == 3
    assert request.filters.include_images


def test_image_and_text_response_shapes():
    file = SimpleNamespace(id=uuid4(), thread_id=uuid4(), file_name='graph.png')
    image = SimpleNamespace(id=uuid4(), image_metadata={}, page_no=2, image_type='graph', description='A rent graph')
    p = passage('image', image, file, None)
    assert p.model_derived and p.page_start == p.page_end == 2 and p.image_type == 'graph'
    assert p.node_type is None and p.document_type is None and p.legal_path == []
    text = SimpleNamespace(id=uuid4(), node_metadata={'legal_path': ['Operative', '2']}, page_start=1,
        page_end=2, heading_path='LEASE', node_type='table', content='Monthly rent')
    p = passage('text', text, file, SimpleNamespace(title='Lease', document_type='lease_deed'))
    assert not p.model_derived and p.node_type == 'table' and p.image_type is None
    assert rerank_text(p) == 'LEASE > Operative > 2\nMonthly rent'


def test_cache_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    expected = Path(__file__).resolve().parents[3] / 'kafka/.cache/huggingface/hub'
    monkeypatch.chdir(tmp_path)
    assert cache_dir(settings()) == expected


def test_embedding_matches_kafka_contract_and_mock_vector(monkeypatch):
    worker = Path(__file__).resolve().parents[3] / 'kafka/pyconsumers/helpers/embedding_helper.py'
    if not worker.exists():
        pytest.skip('Needs the consumers repository checked out next to this one (../kafka).')
    spec = importlib.util.spec_from_file_location('worker_embedding', worker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker_settings = Path(__file__).resolve().parents[3] / 'kafka/pyconsumers/helpers/secrets_control.py'
    spec = importlib.util.spec_from_file_location('worker_settings', worker_settings)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    ours, theirs = settings(), config.Settings(_env_file=None, postgres_password='t', valkey_password='t', storage_access_key='t', storage_secret_key='t')
    for key in ('embedding_model', 'embedding_model_revision', 'embedding_max_length', 'embedding_device'):
        assert getattr(ours, key) == getattr(theirs, key)
    calls = []

    class Tensor:
        def __init__(self, value):
            self.value = value
        def __getitem__(self, key):
            assert key == (slice(None), 0)
            return Tensor([row[0] for row in self.value])
        def float(self): return self
        def cpu(self): return self
        def tolist(self): return self.value

    def normalize(tensor, p, dim):
        assert (p, dim) == (2, -1)
        return Tensor([[v / sum(x*x for x in row)**0.5 for v in row] for row in tensor.value])

    class Batch(dict):
        def to(self, device):
            assert device == 'cpu'
            return self

    def tokenizer(texts, **kwargs):
        calls.append((texts, kwargs))
        return Batch()

    torch = SimpleNamespace(inference_mode=nullcontext, nn=SimpleNamespace(functional=SimpleNamespace(normalize=normalize)))
    monkeypatch.setitem(__import__('sys').modules, 'torch', torch)
    outputs = []
    for cls, settings_value in [(BGEM3, ours), (module.BGEM3, theirs)]:
        model = cls(settings_value)
        model._tokenizer = tokenizer
        model._model = lambda **kwargs: SimpleNamespace(last_hidden_state=Tensor([[[3., 4.], [9., 0.]]]))
        model._device = 'cpu'
        outputs.append(model.embed(['identical legal text']))
    assert calls[0] == calls[1]
    assert outputs == [[[0.6, 0.8]], [[0.6, 0.8]]]


async def test_reranker_missing_dependencies_is_local_only(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'transformers', None)
    with pytest.raises(ModelUnavailable):
        await Reranker(settings()).score_async('rent', ['a passage'])
