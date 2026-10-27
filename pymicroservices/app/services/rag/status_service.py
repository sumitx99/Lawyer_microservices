"""PostgreSQL is authoritative; optional Valkey notifications reduce SSE polling delay."""
import asyncio
from contextlib import suppress
import time


class StatusService:
    def __init__(self, documents, cache):
        self.documents, self.cache = documents, cache

    def channel(self, project_id, doc_id):
        return self.cache.key(project_id, 'document-progress', doc_id)

    async def notify(self, document):
        # Cache outages never turn a committed upload into an API failure.
        with suppress(Exception):
            async with asyncio.timeout(1):
                await self.cache.client.publish(self.channel(document.project_id, document.id), 'changed')

    async def stream(self, project_id, doc_id):
        pubsub = None
        last, heartbeat = None, time.monotonic()
        try:
            try:
                async with asyncio.timeout(1):
                    pubsub = self.cache.client.pubsub()
                    await pubsub.subscribe(self.channel(project_id, doc_id))
            except Exception:
                if pubsub:
                    with suppress(Exception):
                        await pubsub.aclose()
                pubsub = None
            while True:
                document = await self.documents.get(project_id, doc_id)
                data = document.model_dump_json()
                if data != last:
                    yield f'id: {document.updated_at.isoformat()}\nevent: status\ndata: {data}\n\n'
                    last = data
                if document.status in {'INDEXED', 'FAILED'}:
                    break
                if time.monotonic() - heartbeat >= 15:
                    yield ': keepalive\n\n'
                    heartbeat = time.monotonic()
                if pubsub:
                    try:
                        await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
                    except Exception:
                        with suppress(Exception):
                            await pubsub.aclose()
                        pubsub = None
                else:
                    await asyncio.sleep(2)
        finally:
            if pubsub:
                with suppress(Exception):
                    await pubsub.aclose()
