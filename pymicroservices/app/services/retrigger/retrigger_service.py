"""/retrigger: put FAILED or stuck stages back to WAITING, reset the stages after them, wake the consumer.

A stage can be retriggered when it is FAILED, RUNNING with an expired lease (its worker died), or
WAITING longer than its stuck threshold. A stage RUNNING with a live lease is refused ("running"),
which stops double clicks and two workers on one file. Everything happens in one transaction with
the folder message queued through the outbox.
"""
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select

from ...models import UnstructuredData
from ...schemas import RetriggerResponse, RetriggerResult
from ...utilities.file_validator import UploadError
from ..threads.unstructured.thread_service import DOWNSTREAM, get_thread, queue_message, refresh_thread_status, stages_for

logger = logging.getLogger(__name__)


class RetriggerService:
    def __init__(self, db, settings, threads):
        self.db, self.settings, self.threads = db, settings, threads

    def verdict(self, row, stage, now):
        """('retrigger' | 'running' | 'skip', reason) for one stage of one file."""
        status = getattr(row, f'{stage}_status')
        if status == 'FAILED':
            return 'retrigger', getattr(row, f'{stage}_error') or 'failed'
        if status == 'RUNNING':
            lease = getattr(row, f'{stage}_lease_expires_at')
            if lease and lease > now:
                return 'running', f'{stage} is running (worker heartbeat is current).'
            return 'retrigger', f'{stage} stalled: its worker stopped renewing the lease.'
        if status == 'WAITING':
            limit = (self.settings.standardization_stuck_seconds if stage == 'standardization'
                     else self.settings.stage_stuck_seconds)
            updated = getattr(row, f'{stage}_updated_at')
            if updated is None or updated < now - timedelta(seconds=limit):
                return 'retrigger', f'{stage} has been waiting longer than {limit // 60} minutes.'
            return 'skip', f'{stage} is already waiting to run.'
        return 'skip', f'{stage} is {(status or "not started").lower()}.'

    def reset(self, row, stage, now):
        for name, status in [(stage, 'WAITING')] + [(later, None) for later in DOWNSTREAM[stage]]:
            setattr(row, f'{name}_status', status)
            setattr(row, f'{name}_attempts', 0)
            setattr(row, f'{name}_error', None)
            setattr(row, f'{name}_owner', None)
            setattr(row, f'{name}_lease_expires_at', None)
            setattr(row, f'{name}_updated_at', now)

    async def retrigger(self, user_id, thread_id, doc_ids=None, stage=None):
        now = datetime.now(timezone.utc)
        results, wake = [], set()
        async with self.db.transaction() as session:
            await get_thread(session, thread_id, lock=True)
            statement = select(UnstructuredData).where(UnstructuredData.thread_id == thread_id)
            if doc_ids:
                statement = statement.where(UnstructuredData.id.in_(doc_ids))
            rows = list(await session.scalars(statement.order_by(UnstructuredData.created_at, UnstructuredData.id)
                                              .with_for_update()))
            if doc_ids and len(rows) != len(set(doc_ids)):
                raise UploadError('One or more files are not in this folder.', 404)
            for row in rows:
                result = self.choose(row, stage, now)
                if result.outcome == 'retriggered':
                    self.reset(row, result.stage, now)
                    wake.add(result.stage)
                results.append(result)
            await session.flush()
            for name in sorted(wake):
                queue_message(session, thread_id, name)
            status = await refresh_thread_status(session, thread_id)
        if wake:
            logger.info('Retrigger by %s on folder %s: %s', user_id, thread_id,
                        [(str(r.doc_id), r.stage) for r in results if r.outcome == 'retriggered'])
            await self.threads.notify(thread_id)
        return RetriggerResponse(thread_id=thread_id, thread_status=status, results=results)

    def choose(self, row, stage, now):
        if row.upload_status != 'COMPLETED':
            return RetriggerResult(doc_id=row.id, outcome='not_retriggerable',
                                   reason='The upload did not complete; upload the file again.')
        order = stages_for(row.file_type)
        if stage is not None and stage not in order:
            return RetriggerResult(doc_id=row.id, outcome='not_retriggerable', stage=stage,
                                   reason=f'{stage} does not apply to {row.file_type} files.')
        running = None
        for name in ((stage,) if stage else order):
            verdict, reason = self.verdict(row, name, now)
            if verdict == 'running':
                running = running or RetriggerResult(doc_id=row.id, outcome='running', stage=name, reason=reason)
            elif verdict == 'retrigger':
                busy = next((later for later in DOWNSTREAM[name] if self.verdict(row, later, now)[0] == 'running'), None)
                if busy:
                    return RetriggerResult(doc_id=row.id, outcome='running', stage=busy,
                                           reason=f'{busy} is running and depends on {name}; retry when it finishes.')
                return RetriggerResult(doc_id=row.id, outcome='retriggered', stage=name, reason=reason)
            elif stage:
                return RetriggerResult(doc_id=row.id, outcome='not_retriggerable', stage=name, reason=reason)
        return running or RetriggerResult(doc_id=row.id, outcome='not_retriggerable',
                                          reason='Nothing has failed or stalled for this file.')
