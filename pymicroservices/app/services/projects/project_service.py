"""Owner-scoped projects and folder links."""
from collections import defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import load_only

from ...models import DataFabric, FabricUnstructuredThread, UnstructuredData, UnstructuredThread
from ...schemas import (ProjectAttachResponse, ProjectDeleteResponse, ProjectDetailResponse, ProjectDetachResponse,
                        ProjectFolderResponse, ProjectListResponse, ProjectResponse, ProjectSummary)
from ...utilities.file_validator import UploadError
from ..threads.unstructured.thread_service import derive_thread_status, file_state


class ProjectService:
    def __init__(self, db):
        self.db = db

    async def project(self, session, user_id, project_id, lock=False):
        query = select(DataFabric).where(DataFabric.id == project_id, DataFabric.created_by == user_id)
        project = await session.scalar(query.with_for_update() if lock else query)
        if project is None:
            raise UploadError('Project not found.', 404)
        return project

    async def owned_threads(self, session, user_id, thread_ids):
        ids = list(dict.fromkeys(thread_ids))
        owned = list(await session.scalars(select(UnstructuredThread.id).where(
            UnstructuredThread.id.in_(ids), UnstructuredThread.created_by == user_id)
            .order_by(UnstructuredThread.id).with_for_update()))
        if len(owned) != len(ids):
            raise UploadError('Folder not found.', 404)
        return ids

    async def contents(self, session, user_id, project_ids):
        folders = (await session.execute(select(FabricUnstructuredThread.fabric_id, UnstructuredThread,
                                                FabricUnstructuredThread.created_at)
            .join(UnstructuredThread, UnstructuredThread.id == FabricUnstructuredThread.thread_id)
            .join(DataFabric, DataFabric.id == FabricUnstructuredThread.fabric_id)
            .where(DataFabric.id.in_(project_ids), DataFabric.created_by == user_id,
                   UnstructuredThread.created_by == user_id)
            .order_by(FabricUnstructuredThread.created_at.desc(), UnstructuredThread.id.desc()))).all()
        files = defaultdict(list)
        thread_ids = {thread.id for _, thread, _ in folders}
        if thread_ids:
            columns = [UnstructuredData.file_type, UnstructuredData.upload_status, UnstructuredData.thread_id]
            columns += [getattr(UnstructuredData, f'{stage}_status') for stage in
                        ('standardization', 'markdown', 'image', 'metaclassifier', 'parquet')]
            rows = await session.scalars(select(UnstructuredData).join(UnstructuredThread)
                .where(UnstructuredData.thread_id.in_(thread_ids), UnstructuredThread.created_by == user_id)
                .options(load_only(*columns)))
            for row in rows:
                files[row.thread_id].append(row)
        return folders, files

    async def create(self, user_id, request):
        async with self.db.transaction() as session:
            project = DataFabric(name=request.name, description=request.description, created_by=user_id)
            session.add(project)
            await session.flush()
            return ProjectResponse.model_validate(project)

    async def list(self, user_id):
        async with self.db.transaction() as session:
            projects = list(await session.scalars(select(DataFabric).where(DataFabric.created_by == user_id)
                .order_by(DataFabric.created_at.desc(), DataFabric.id.desc())))
            if not projects:
                return ProjectListResponse(items=[])
            folders, files = await self.contents(session, user_id, [project.id for project in projects])
            grouped = defaultdict(list)
            for project_id, thread, _ in folders:
                grouped[project_id].append(thread.id)
            items = []
            for project in projects:
                counts = {'processing': 0, 'done': 0, 'failed': 0}
                for thread_id in grouped[project.id]:
                    for file in files[thread_id]:
                        state = file_state(file)
                        counts['processing' if state == 'uploading' else state] += 1
                items.append(ProjectSummary(**ProjectResponse.model_validate(project).model_dump(),
                    folder_count=len(grouped[project.id]), file_count=sum(counts.values()), file_counts=counts))
            return ProjectListResponse(items=items)

    async def get(self, user_id, project_id):
        async with self.db.transaction() as session:
            project = await self.project(session, user_id, project_id)
            folders, files = await self.contents(session, user_id, [project_id])
            return ProjectDetailResponse(**ProjectResponse.model_validate(project).model_dump(), folders=[
                ProjectFolderResponse(id=thread.id, name=thread.name, status=derive_thread_status(files[thread.id]),
                                      file_count=len(files[thread.id]), attached_at=attached_at)
                for _, thread, attached_at in folders])

    async def update(self, user_id, request):
        async with self.db.transaction() as session:
            project = await self.project(session, user_id, request.project_id, lock=True)
            values = request.model_dump(exclude_unset=True, exclude={'project_id'})
            if values:
                project = await session.scalar(update(DataFabric).where(
                    DataFabric.id == request.project_id, DataFabric.created_by == user_id)
                    .values(**values).returning(DataFabric))
            return ProjectResponse.model_validate(project)

    async def attach(self, user_id, request):
        async with self.db.transaction() as session:
            await self.project(session, user_id, request.project_id, lock=True)
            ids = await self.owned_threads(session, user_id, request.thread_ids)
            inserted = set(await session.scalars(insert(FabricUnstructuredThread).values([
                {'fabric_id': request.project_id, 'thread_id': thread_id} for thread_id in ids])
                .on_conflict_do_nothing().returning(FabricUnstructuredThread.thread_id)))
            return ProjectAttachResponse(project_id=request.project_id, attached=[i for i in ids if i in inserted],
                                         already_attached=[i for i in ids if i not in inserted])

    async def detach(self, user_id, request):
        async with self.db.transaction() as session:
            await self.project(session, user_id, request.project_id, lock=True)
            ids = await self.owned_threads(session, user_id, request.thread_ids)
            removed = set(await session.scalars(delete(FabricUnstructuredThread).where(
                FabricUnstructuredThread.fabric_id == request.project_id, FabricUnstructuredThread.thread_id.in_(ids))
                .returning(FabricUnstructuredThread.thread_id)))
            return ProjectDetachResponse(project_id=request.project_id, detached=[i for i in ids if i in removed])

    async def delete(self, user_id, project_id):
        async with self.db.transaction() as session:
            await self.project(session, user_id, project_id, lock=True)
            await session.execute(delete(DataFabric).where(DataFabric.id == project_id, DataFabric.created_by == user_id))
            return ProjectDeleteResponse(project_id=project_id, deleted=True)
