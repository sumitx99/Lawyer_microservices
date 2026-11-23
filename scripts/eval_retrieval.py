"""Evaluate retrieval on a temporary fictional corpus; never invoke a generative model or edit workers.

Run from microservices with its venv; the fixtures are parsed, chunked and embedded by a child process in
the kafka venv (Docling lives only there), exactly as the indexing workers would:

    .venv/bin/python scripts/eval_retrieval.py --reranker off   # RRF only
    .venv/bin/python scripts/eval_retrieval.py --reranker on    # with bge-reranker-v2-m3
    make eval-retrieval                                         # both modes in one process

cold_start_ms is the first search of a mode: model loading (BGE-M3, plus the reranker when on) and the
first inference. Warm p50/p95 cover 3 rounds of every question after it. The corpus is owned by a random
`retrieval-eval-*` user and deleted in `finally`.
"""
import argparse
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
KAFKA = ROOT.parent / 'kafka'
HINDI = [
    'शपथपत्र',
    '1. मैं, सीमा देवी, पुत्री मोहन प्रसाद, निवासी राजेंद्र नगर, पटना, यह शपथपत्र देती हूँ।',
    '2. मेरा जन्म 12 मार्च 1980 को हुआ था। मेरी आयु छियालीस वर्ष है।',
    '3. मैं राजेंद्र नगर स्थित मकान संख्या 18 की स्वामी हूँ। यह मकान मेरा स्थायी निवास है।',
    '4. मेरी मासिक आय 30000 रुपये है। यह आय मेरी सिलाई की दुकान से प्राप्त होती है।',
    '5. मैंने अपने पुत्र की शिक्षा के लिए 50000 रुपये की बचत की है।',
    '6. यह शपथपत्र विद्यालय में निवास प्रमाण प्रस्तुत करने के लिए दिया जा रहा है।',
    '7. सत्यापन: उपर्युक्त कथन मेरे निजी ज्ञान के अनुसार सत्य हैं। पटना, दिनांक 1 अप्रैल 2026।']
QUESTIONS = [
    (0, 'Who are the lessor and lessee?', 'Asha Verma'),
    (0, 'What is the monthly rent?', 'monthly rent of Rs. 15,000'),
    (0, 'When must rent be paid?', '5th day'),
    (0, 'How much is the security deposit?', 'security deposit of Rs. 45,000'),
    (0, 'How long is the lease term?', 'eleven months'),
    (0, 'What notice is required for termination?', 'one month written notice'),
    (0, 'What does Clause 4 say about maintenance?', 'structural changes'),
    (0, 'Which courts have jurisdiction over the rent agreement?', 'courts at Patna'),
    (1, 'Who is the complainant?', 'Meera Sinha'),
    (1, 'Who is the accused?', 'Arun Prakash'),
    (1, 'Which statute is invoked in the cheque complaint?', 'Section 138'),
    (1, 'What is the case number 1234 of 2025?', '1234 of 2025'),
    (1, 'How much was the dishonoured cheque?', '2,50,000'),
    (1, 'How much compensation was ordered?', '3,00,000'),
    (2, 'शपथपत्र देने वाली महिला का नाम क्या है?', 'सीमा देवी'),
    (2, 'सीमा देवी का जन्म कब हुआ?', '12 मार्च 1980'),
    (2, 'सीमा देवी कहाँ रहती हैं?', 'मकान संख्या 18'),
    (2, 'सीमा देवी की मासिक आय कितनी है?', '30000 रुपये'),
    (2, 'बच्चे की शिक्षा के लिए कितनी बचत की गई है?', '50000 रुपये'),
    (2, 'यह शपथपत्र किस उद्देश्य से दिया गया है?', 'विद्यालय')]


async def prepare():
    sys.path.insert(0, str(KAFKA))
    from pyconsumers.core.rag_pipeline.connectors.unstructured.utils.parser import DoclingParser
    from pyconsumers.core.rag_pipeline.chunking.text_chunker import build_sections, chunk_sections
    from pyconsumers.core.rag_pipeline.chunking.table_chunker import chunk_tables
    from pyconsumers.core.rag_pipeline.execute_indexing import table_captions, text_search_config
    from pyconsumers.core.rag_pipeline.schemas.parsed import ParsedBlock, ParserOutput
    from pyconsumers.helpers.embedding_helper import BGEM3
    from pyconsumers.helpers.secrets_control import get_settings
    settings = get_settings()
    model = BGEM3(settings)
    artifacts = []
    with TemporaryDirectory(prefix='lexaior-eval-') as folder:
        for number, name in enumerate(['lease_deed.pdf', 'court_order.pdf', 'hindi_affidavit.txt']):
            if number < 2:
                artifact = await DoclingParser(settings).parse(KAFKA / 'tests/fixtures' / name, Path(folder) / str(number))
            else:
                artifact = ParserOutput(parser='text', parser_version='1', docling_document={}, blocks=[
                    ParsedBlock(ref=f'#/texts/{n}', label='section_header' if n == 0 else 'text', text=line)
                    for n, line in enumerate(HINDI)])
            sections, _, tables = build_sections(artifact.blocks, table_captions(artifact))
            budget = (model.count_tokens, settings.chunk_target_tokens, settings.chunk_max_tokens)
            chunks = chunk_sections(sections, *budget) + chunk_tables(tables, *budget)
            chunks.sort(key=lambda c: (c.section_ordinal, c.position, c.ordinal))
            vectors = await model.embed_async([c.embedding_text for c in chunks])
            config, _ = text_search_config([c.content for c in chunks])
            artifacts.append({'file_name': name, 'config': config, 'chunks': [
                {**asdict(c), 'vector': vector} for c, vector in zip(chunks, vectors, strict=True)]})
    print(json.dumps(artifacts, ensure_ascii=False))


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    left, right = int(position), min(int(position) + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)


async def evaluate(modes):
    sys.path.insert(0, str(ROOT))
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, func, select
    from pymicroservices.app.main import create_app
    from pymicroservices.app.models import (DataFabric, FabricUnstructuredThread, NodeRelationship, TextNode,
                                           UnstructuredData, UnstructuredMetadata, UnstructuredThread)
    from pymicroservices.app.utilities.secrets_control import get_settings
    settings = get_settings().model_copy(update={'outbox_enabled': False, 'upload_cleanup_enabled': False,
                                                'retrieval_warmup': False})
    start = perf_counter()
    child = await asyncio.create_subprocess_exec(str(KAFKA / '.venv/bin/python'), str(Path(__file__).resolve()),
        '--prepare', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=KAFKA, env=os.environ | {'HF_HUB_OFFLINE': '1'})
    stdout, stderr = await child.communicate()
    if child.returncode:
        raise RuntimeError('Fixture preparation failed: ' + stderr.decode()[-2000:])
    artifacts = json.loads(stdout)
    preparation = (perf_counter() - start) * 1000
    owner, project = 'retrieval-eval-' + uuid4().hex, uuid4()
    docs, threads = [uuid4() for _ in artifacts], [uuid4() for _ in artifacts]
    gold, stored = [], []
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        try:
            async with app.state.db.transaction() as session:
                session.add(DataFabric(id=project, name='Temporary retrieval evaluation', created_by=owner))
                session.add_all([UnstructuredThread(id=id, name=a['file_name'], created_by=owner, status='COMPLETED')
                                 for id, a in zip(threads, artifacts)])
                await session.flush()
                session.add_all([FabricUnstructuredThread(fabric_id=project, thread_id=t) for t in threads])
                for number, artifact in enumerate(artifacts):
                    session.add(UnstructuredData(id=docs[number], thread_id=threads[number], file_name=artifact['file_name'],
                        file_type='txt' if number == 2 else 'pdf', category='text' if number == 2 else 'document', byte_count=1,
                        sha256=sha256(artifact['file_name'].encode()).hexdigest(), s3_path=f'retrieval-eval/{project}/{number}',
                        upload_status='COMPLETED', standardization_status='COMPLETED', markdown_status='COMPLETED',
                        image_status='INAPPLICABLE', metaclassifier_status='COMPLETED'))
                await session.flush()
                for number, artifact in enumerate(artifacts):
                    ids = [uuid4() for _ in artifact['chunks']]
                    for index, (id, chunk) in enumerate(zip(ids, artifact['chunks'], strict=True)):
                        session.add(TextNode(id=id, doc_id=docs[number], thread_id=threads[number], chunk_index=index,
                            total_chunks=len(ids), node_type=chunk['chunk_type'], heading_path=' > '.join(chunk['heading_path']),
                            content=chunk['content'], token_count=chunk['token_count'], page_start=chunk['page_start'],
                            page_end=chunk['page_end'], table_id=chunk['table_id'], node_metadata=chunk['source_metadata'],
                            ts_config=artifact['config'], embedding=chunk['vector']))
                        stored.append((number, str(id), chunk['content']))
                    await session.flush()
                    for before, after in zip(ids, ids[1:]):
                        for source, target, kind in [(before, after, 'NEXT'), (after, before, 'PREVIOUS')]:
                            session.add(NodeRelationship(doc_id=docs[number], source_node_id=source, target_node_id=target,
                                                         relationship=kind))
                    session.add(UnstructuredMetadata(doc_id=docs[number], thread_id=threads[number],
                        title=['RENT AGREEMENT', 'COURT ORDER', 'शपथपत्र'][number],
                        document_type=['lease_deed', 'court_order', 'affidavit'][number], jurisdiction='Patna',
                        case_number='1234 of 2025' if number == 1 else None,
                        statutes_cited=['Section 138 of the Negotiable Instruments Act, 1881'] if number == 1 else []))
            for number, question, needle in QUESTIONS:
                correct = {id for file, id, body in stored if file == number and
                           ' '.join(needle.lower().split()) in ' '.join(body.lower().split())}
                if not correct:
                    raise ValueError(f'Gold passage not found: {needle}')
                gold.append((question, correct))
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://eval',
                                   headers={'X-User-Id': owner}) as client:
                report = {'questions': len(gold), 'fixture_preparation_ms': preparation, 'modes': {}}
                for enabled in modes:
                    app.state.retrieval.settings = settings.model_copy(update={'reranker_enabled': enabled})
                    payload = {'project_id': str(project), 'query': gold[0][0], 'top_k': 8}
                    start = perf_counter()
                    first = await client.post('/retrieval/search', json=payload)
                    first.raise_for_status()
                    cold = (perf_counter() - start) * 1000
                    if enabled and not first.json()['reranked']:
                        raise RuntimeError('Reranker is unavailable; evaluation requires both modes.')
                    recalls, reciprocals, latencies = [], [], []
                    for _ in range(3):
                        for question, correct in gold:
                            payload['query'] = question
                            start = perf_counter()
                            response = await client.post('/retrieval/search', json=payload)
                            response.raise_for_status()
                            latencies.append((perf_counter() - start) * 1000)
                            body = response.json()
                            if enabled and not body['reranked']:
                                raise RuntimeError('Reranker became unavailable.')
                            ids = [row['id'] for row in body['results']]
                            recalls.append(len(set(ids) & correct) / len(correct))
                            reciprocals.append(next((1 / rank for rank, id in enumerate(ids, 1) if id in correct), 0))
                    report['modes']['reranked' if enabled else 'rrf'] = {
                        'recall_at_8': statistics.mean(recalls), 'mrr': statistics.mean(reciprocals),
                        'p50_ms': percentile(latencies, .50), 'p95_ms': percentile(latencies, .95), 'cold_start_ms': cold}
                print(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            async with app.state.db.transaction() as session:
                await session.execute(delete(DataFabric).where(DataFabric.id == project, DataFabric.created_by == owner))
                await session.execute(delete(UnstructuredThread).where(UnstructuredThread.created_by == owner))
            async with app.state.db.transaction() as session:
                assert not await session.scalar(select(func.count()).select_from(UnstructuredData).where(UnstructuredData.id.in_(docs)))
                assert not await session.scalar(select(func.count()).select_from(DataFabric).where(DataFabric.id == project))
            print('Evaluation corpus cleaned up.', file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--reranker', choices=['off', 'on', 'both'], default='both')
    args = parser.parse_args()
    modes = {'off': (False,), 'on': (True,), 'both': (False, True)}[args.reranker]
    asyncio.run(prepare() if args.prepare else evaluate(modes))
