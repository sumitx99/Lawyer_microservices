"""Project-scoped hybrid retrieval and evidence inspection, without answer generation."""
from collections import Counter
from copy import deepcopy
import math
import re
from time import perf_counter
from uuid import UUID

from sqlalchemy import and_, case, cast, func, or_, select, Text, text
from sqlalchemy.orm import defer

from ...models import DataFabric, FabricUnstructuredThread, ImageRagVector, TextNode, UnstructuredData, UnstructuredMetadata, UnstructuredThread
from ...schemas import (RetrievalDocument, RetrievalDocumentsResponse, RetrievalExpandResponse, RetrievalFactsResponse,
                        RetrievalPassage, RetrievalSearchResponse, RetrievalTiming, StageState)
from ...utilities.file_validator import UploadError
from ...utilities.retrieval_models import ModelUnavailable

NUMBER = r'\d+(?:\.\d+)*(?:[a-z])?(?:\(\d+\))*'
REFERENCE_PATTERNS = [
    ('section', rf'(?:\bsection\b|\bs\.|धारा)\s*({NUMBER})'),
    ('article', rf'\barticle\s+({NUMBER})'), ('clause', rf'\bclause\s+({NUMBER})'),
    ('order', rf'\border\s+({NUMBER})\s+rule\s+({NUMBER})'),
    ('case', r'\b(\d+)\s+of\s+(\d{4})')]


def references(query):
    query = ''.join(str(int(ch)) if ch.isdecimal() else ch for ch in query)
    found = []
    for kind, pattern in REFERENCE_PATTERNS:
        for match in re.finditer(pattern + r'(?![\w]|\.\d)', query, re.I):
            numbers = match.groups()
            label = {'section': r'(section|s[.]|धारा)', 'article': 'article', 'clause': 'clause'}
            if kind in label:
                body = label[kind] + '[[:space:]]*' + re.escape(numbers[0])
            elif kind == 'order':
                body = 'order[[:space:]]+' + re.escape(numbers[0]) + '[[:space:]]+rule[[:space:]]+' + re.escape(numbers[1])
            else:
                body = re.escape(numbers[0]) + '[[:space:]]+of[[:space:]]+' + numbers[1]
            ref = {'kind': kind, 'numbers': numbers,
                   'pattern': r'(^|[^[:alnum:]_])' + body + r'(?![[:alnum:]_]|[.][0-9])'}
            if ref not in found:
                found.append(ref)
    return found


def fuse(lists, k=60, limit=50, has_references=False):
    found = {}
    for source, weight in [('vector', 1), ('keyword', 1), ('reference', 2)]:
        seen = set()
        for rank, item in enumerate(lists[source], 1):
            key = (item.kind, item.id)
            if key in seen:
                continue
            seen.add(key)
            if key not in found:
                found[key] = item.model_copy(deep=True)
                found[key].scores.rrf = 0
                found[key].scores.reference_match = False if has_references else None
            scores = found[key].scores
            scores.rrf += weight / (k + rank)
            if source == 'reference':
                scores.reference_match = True
            else:
                setattr(scores, f'{source}_rank', rank)
    return sorted(found.values(), key=lambda p: (-p.scores.rrf, p.kind, str(p.id)))[:limit]


def diversify(items, top_k, max_per_document):
    counts, selected = Counter(), []
    for item in items:
        if counts[item.doc_id] < max_per_document:
            counts[item.doc_id] += 1
            selected.append(item)
            if len(selected) == top_k:
                break
    return selected


def passage(kind, node, file, metadata):
    image = kind == 'image'
    attrs = node.image_metadata if image else node.node_metadata
    path = attrs.get('legal_path') or []
    return RetrievalPassage(kind=kind, id=node.id, doc_id=file.id, thread_id=file.thread_id, file_name=file.file_name,
        document_type=metadata.document_type if metadata else None, title=metadata.title if metadata else None,
        page_start=node.page_no if image else node.page_start, page_end=node.page_no if image else node.page_end,
        heading_path='' if image else node.heading_path, legal_path=[str(p) for p in path],
        node_type=None if image else node.node_type, image_type=node.image_type if image else None,
        content=node.description if image else node.content, model_derived=image or bool(attrs.get('model_derived')))


def rerank_text(item):
    return ' > '.join(([item.heading_path] if item.heading_path else []) + item.legal_path) + '\n' + item.content


class RetrievalService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def threads(self, user_id, project_id):
        return select(UnstructuredThread.id).select_from(DataFabric).join(FabricUnstructuredThread,
            FabricUnstructuredThread.fabric_id == DataFabric.id).join(UnstructuredThread,
            UnstructuredThread.id == FabricUnstructuredThread.thread_id).where(
            DataFabric.id == project_id, DataFabric.created_by == user_id, UnstructuredThread.created_by == user_id)

    def scope(self, user_id, project_id):
        return select(UnstructuredData.id).where(UnstructuredData.thread_id.in_(self.threads(user_id, project_id)))

    async def authorize(self, session, user_id, project_id, filters=None):
        if not await session.scalar(select(DataFabric.id).where(DataFabric.id == project_id, DataFabric.created_by == user_id)):
            raise UploadError('Project not found.', 404)
        if filters:
            for ids, scoped, column in [(filters.thread_ids, self.threads(user_id, project_id), UnstructuredThread.id),
                                        (filters.doc_ids, self.scope(user_id, project_id), UnstructuredData.id)]:
                if ids and len(set(await session.scalars(scoped.where(column.in_(ids))))) != len(set(ids)):
                    raise UploadError('Folder or file not found.', 404)

    def filters(self, query, filters):
        for field in ('jurisdiction', 'court'):
            if getattr(filters, field) is not None:
                query = query.where(getattr(UnstructuredMetadata, field) == getattr(filters, field))
        if filters.document_types:
            query = query.where(UnstructuredMetadata.document_type.in_(filters.document_types))
        if filters.date_from:
            query = query.where(UnstructuredMetadata.decision_date >= filters.date_from)
        if filters.date_to:
            query = query.where(UnstructuredMetadata.decision_date <= filters.date_to)
        if filters.thread_ids:
            query = query.where(UnstructuredData.thread_id.in_(filters.thread_ids))
        if filters.doc_ids:
            query = query.where(UnstructuredData.id.in_(filters.doc_ids))
        return query

    def nodes(self, kind, user_id, project_id, filters=None):
        node = TextNode if kind == 'text' else ImageRagVector
        ready = UnstructuredData.markdown_status if kind == 'text' else UnstructuredData.image_status
        query = select(node, UnstructuredData, UnstructuredMetadata).join(UnstructuredData,
            and_(UnstructuredData.id == node.doc_id, UnstructuredData.thread_id == node.thread_id))
        query = query.outerjoin(UnstructuredMetadata, and_(UnstructuredMetadata.doc_id == UnstructuredData.id,
            UnstructuredMetadata.thread_id == UnstructuredData.thread_id)).where(
            UnstructuredData.id.in_(self.scope(user_id, project_id)), ready == 'COMPLETED')
        query = query.options(defer(node.embedding), defer(node.tsv), defer(UnstructuredMetadata.title_embedding))
        return self.filters(query, filters) if filters else query

    async def candidate_lists(self, session, user_id, request, vector, refs):
        await session.execute(text('SET LOCAL hnsw.ef_search = 100'))
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        lists = {'vector': [], 'keyword': [], 'reference': []}
        limit = self.settings.retrieval_candidates
        for kind in ['text'] + (['image'] if request.filters.include_images else []):
            node = TextNode if kind == 'text' else ImageRagVector
            base = self.nodes(kind, user_id, request.project_id, request.filters)
            distance = node.embedding.cosine_distance(vector)
            rows = await session.execute(base.add_columns(distance.label('distance'))
                .order_by(distance).limit(limit))
            lists['vector'] += [(float(row[3]), passage(kind, *row[:3])) for row in rows]
            config = TextNode.ts_config if kind == 'text' else text("'simple'::regconfig")
            tsquery = func.websearch_to_tsquery(config, request.query)
            rank = func.ts_rank_cd(node.tsv, tsquery)
            # Constant-config prefilters let Postgres use the GIN index for text rows.
            match = node.tsv.op('@@')(tsquery)
            if kind == 'text':
                match = and_(match, or_(*[and_(TextNode.ts_config == text(f"'{cfg}'::regconfig"),
                    node.tsv.op('@@')(func.websearch_to_tsquery(text(f"'{cfg}'::regconfig"), request.query)))
                    for cfg in ('english', 'simple')]))
            rows = await session.execute(base.add_columns(rank.label('keyword_score')).where(match)
                .order_by(rank.desc(), node.id).limit(limit))
            lists['keyword'] += [(-float(row[3]), passage(kind, *row[:3])) for row in rows]
            if refs:
                direct, metadata = [], []
                for ref in refs:
                    body = node.content if kind == 'text' else node.description
                    tests = [body.op('~*')(ref['pattern'])]
                    if kind == 'text' and ref['kind'] != 'case':
                        tests.append(and_(*[TextNode.node_metadata['legal_path'].contains([n]) for n in ref['numbers']]))
                    direct.append(or_(*tests))
                    metadata += [UnstructuredMetadata.case_number.op('~*')(ref['pattern']),
                                 cast(UnstructuredMetadata.statutes_cited, Text).op('~*')(ref['pattern'])]
                direct_match = or_(*direct)
                priority = case((direct_match, 0), else_=1)
                rows = await session.execute(base.add_columns(priority.label('priority'))
                    .where(or_(direct_match, *metadata)).order_by(priority, node.id).limit(limit))
                lists['reference'] += [(int(row[3]), passage(kind, *row[:3])) for row in rows]
        for source, rows in lists.items():
            lists[source] = [item for _, item in sorted(rows, key=lambda r: (r[0], r[1].kind, str(r[1].id)))[:limit]]
        return lists

    async def search(self, user_id, request, embedder, reranker):
        started, timing = perf_counter(), RetrievalTiming()
        async with self.db.transaction() as session:
            await self.authorize(session, user_id, request.project_id, request.filters)
        before = perf_counter()
        try:
            vectors = await embedder.embed_async([request.query])
            if len(vectors) != 1 or len(vectors[0]) != 1024 or not all(math.isfinite(v) for v in vectors[0]):
                raise ValueError('Invalid query vector.')
        except Exception:
            raise UploadError('Query embedding is unavailable; check the local model cache and dependencies.', 503) from None
        timing.embed = (perf_counter() - before) * 1000
        before = perf_counter()
        refs = references(request.query)
        async with self.db.transaction() as session:
            await self.authorize(session, user_id, request.project_id, request.filters)
            candidates = await self.candidate_lists(session, user_id, request, vectors[0], refs)
        items = fuse(candidates, self.settings.retrieval_rrf_k, self.settings.retrieval_candidates, bool(refs))
        timing.candidates = (perf_counter() - before) * 1000
        reranked = False
        if items and self.settings.reranker_enabled:
            before = perf_counter()
            try:
                scores = await reranker.score_async(request.query, [rerank_text(item) for item in items])
            except ModelUnavailable:
                scores = None
            except Exception:
                raise UploadError('Reranker inference is unavailable.', 503) from None
            if scores is not None:
                if len(scores) != len(items) or not all(math.isfinite(s) for s in scores):
                    raise UploadError('Reranker returned invalid scores.', 503)
                for item, score in zip(items, scores):
                    item.scores.rerank = float(score)
                items.sort(key=lambda item: (-item.scores.rerank, -item.scores.rrf, item.kind, str(item.id)))
                reranked = True
                timing.rerank = (perf_counter() - before) * 1000
        timing.total = (perf_counter() - started) * 1000
        return RetrievalSearchResponse(query=request.query, reranked=reranked, timing_ms=timing,
            results=diversify(items, request.top_k, request.max_per_document))

    async def list_documents(self, user_id, request):
        async with self.db.transaction() as session:
            await self.authorize(session, user_id, request.project_id, request.filters)
            ready = UnstructuredData.markdown_status == 'COMPLETED'
            if request.filters.include_images:
                ready = or_(ready, UnstructuredData.image_status == 'COMPLETED')
            query = select(UnstructuredData, UnstructuredMetadata).outerjoin(UnstructuredMetadata,
                UnstructuredMetadata.doc_id == UnstructuredData.id).where(
                UnstructuredData.id.in_(self.scope(user_id, request.project_id)), ready)
            rows = await session.execute(self.filters(query, request.filters)
                .order_by(UnstructuredData.created_at.desc(), UnstructuredData.id.desc()).limit(request.limit).offset(request.offset))
            items = []
            for file, metadata in rows:
                fields = {key: getattr(metadata, key) for key in ('title', 'document_type', 'jurisdiction', 'court',
                    'case_number', 'parties', 'decision_date', 'summary')} if metadata else {}
                stages = {name: StageState(status=getattr(file, f'{name}_status'), attempts=getattr(file, f'{name}_attempts'),
                    error=getattr(file, f'{name}_error'), updated_at=getattr(file, f'{name}_updated_at'))
                    for name in ('standardization', 'markdown', 'image', 'metaclassifier', 'parquet')}
                items.append(RetrievalDocument(doc_id=file.id, file_name=file.file_name, file_type=file.file_type,
                    thread_id=file.thread_id, stages=stages, **fields))
            return RetrievalDocumentsResponse(items=items, limit=request.limit, offset=request.offset)

    async def document_facts(self, user_id, request):
        async with self.db.transaction() as session:
            await self.authorize(session, user_id, request.project_id)
            row = await session.scalar(select(UnstructuredMetadata).join(UnstructuredData,
                UnstructuredData.id == UnstructuredMetadata.doc_id).where(
                UnstructuredData.id.in_(self.scope(user_id, request.project_id)), UnstructuredData.id == request.doc_id,
                or_(UnstructuredData.markdown_status == 'COMPLETED', UnstructuredData.image_status == 'COMPLETED')))
            if row is None:
                raise UploadError('Document facts not found.', 404)
            response = RetrievalFactsResponse.model_validate(row)
            evidence = deepcopy(response.evidence)
            resolved = {}
            for kind in ('text', 'image'):
                ids = set()
                for entries in evidence.values():
                    for entry in entries:
                        if entry.get('kind') == kind:
                            try:
                                ids.add(UUID(entry['id']))
                            except (ValueError, TypeError, KeyError):
                                pass
                node = TextNode if kind == 'text' else ImageRagVector
                rows = await session.execute(self.nodes(kind, user_id, request.project_id).where(
                    node.doc_id == request.doc_id, node.id.in_(ids)))
                for item, _, _ in rows:
                    resolved[kind, str(item.id)] = item
            for entries in evidence.values():
                for entry in entries:
                    node = resolved.get((entry.get('kind'), entry.get('id')))
                    entry['page'] = None if node is None else node.page_start if entry['kind'] == 'text' else node.page_no
                    entry['chunk_index'] = node.chunk_index if node is not None and entry['kind'] == 'text' else None
                    if entry.get('kind') == 'image':
                        entry['image_index'] = node.image_index if node is not None else None
            response.evidence = evidence
            return response

    async def expand(self, user_id, request):
        async with self.db.transaction() as session:
            await self.authorize(session, user_id, request.project_id)
            base = self.nodes('text', user_id, request.project_id)
            anchor = (await session.execute(base.where(TextNode.id == request.node_id))).first()
            if anchor is None:
                raise UploadError('Passage not found.', 404)
            node = anchor[0]
            query = base.where(TextNode.doc_id == node.doc_id)
            if request.whole_clause:
                path = node.node_metadata.get('legal_path') or []
                prefix = path[:2] if path and path[0] in {'Operative', 'Recital'} else path[:1]
                if prefix:
                    query = query.where(and_(*[TextNode.node_metadata['legal_path'][i].astext == value
                                               for i, value in enumerate(prefix)]))
                else:
                    query = query.where(TextNode.id == node.id)
            else:
                query = query.where(TextNode.chunk_index.between(node.chunk_index - request.before,
                                                                 node.chunk_index + request.after))
            nodes = [passage('text', *row) for row in await session.execute(query.order_by(TextNode.chunk_index))]
            return RetrievalExpandResponse(node_id=node.id, doc_id=node.doc_id, nodes=nodes)
