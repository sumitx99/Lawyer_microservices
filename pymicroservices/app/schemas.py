"""Public API contracts; storage credentials and internal object keys are not exposed."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: UUID
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class DocumentIdentityRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: UUID
    doc_id: UUID


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    filename: str
    media_type: str
    byte_count: int
    sha256: str
    status: str
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    parse_summary: dict = Field(default_factory=dict)


class DocumentList(BaseModel):
    items: list[DocumentResponse]
    limit: int
    offset: int


# ---- Folder pipeline (design doc) -----------------------------------------------------------

Stage = Literal['standardization', 'markdown', 'image', 'metaclassifier', 'parquet']


class DeclaredFileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_name: str = Field(min_length=1, max_length=255)
    byte_count: int
    sha256: str = Field(description='Lowercase hex SHA-256 of the file, computed in the browser.')


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=255)
    data_fabric_id: UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=50)
    jurisdiction: str | None = Field(default=None, max_length=64)
    files: list[DeclaredFileRequest] = Field(min_length=1)


class UploadTarget(BaseModel):
    doc_id: UUID
    file_name: str
    file_type: str
    method: Literal['PUT'] = 'PUT'
    upload_url: str
    headers: dict[str, str] = Field(description='Send exactly these headers with the PUT.')
    expires_at: datetime


class SkippedFile(BaseModel):
    file_name: str
    reason: str


class ThreadCreateResponse(BaseModel):
    thread_id: UUID
    status: str
    files: list[UploadTarget]
    skipped: list[SkippedFile]


class ThreadIdentityRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    thread_id: UUID


class CompleteUploadRequest(ThreadIdentityRequest):
    doc_ids: list[UUID] | None = Field(default=None, description='Defaults to every file still uploading.')


class FileUploadResult(BaseModel):
    doc_id: UUID
    file_name: str
    upload_status: Literal['RUNNING', 'COMPLETED', 'FAILED'] = Field(
        description='RUNNING means the file has not reached storage yet; call again after the PUT.')
    upload_error: str | None = None


class CompleteUploadResponse(BaseModel):
    thread_id: UUID
    thread_status: str
    files: list[FileUploadResult]


class StageState(BaseModel):
    status: str | None
    attempts: int
    error: str | None
    updated_at: datetime | None


class FileStatus(BaseModel):
    doc_id: UUID
    file_name: str
    file_type: str
    category: str
    byte_count: int
    sha256: str
    upload_status: str
    upload_error: str | None
    stages: dict[str, StageState] = Field(description='Only the stages that apply to this file type.')
    created_at: datetime
    updated_at: datetime


class ThreadStatusResponse(BaseModel):
    thread_id: UUID
    name: str
    created_by: str
    status: str
    tags: list[str]
    jurisdiction: str | None
    created_at: datetime
    updated_at: datetime
    files: list[FileStatus]


class RetriggerRequest(ThreadIdentityRequest):
    doc_ids: list[UUID] | None = Field(default=None, description='Defaults to every file in the folder.')
    stage: Stage | None = Field(default=None, description='Defaults to the earliest failed or stuck stage.')


class RetriggerResult(BaseModel):
    doc_id: UUID
    outcome: Literal['retriggered', 'running', 'not_retriggerable']
    stage: str | None = None
    reason: str


class RetriggerResponse(BaseModel):
    thread_id: UUID
    thread_status: str
    results: list[RetriggerResult]


# ---- Projects (data_fabric) -----------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProjectIdentityRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: UUID


class ProjectUpdateRequest(ProjectIdentityRequest):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None

    @model_validator(mode='after')
    def name_not_null(self):
        if 'name' in self.model_fields_set and self.name is None:
            raise ValueError('Project name cannot be null.')
        return self


class ProjectThreadsRequest(ProjectIdentityRequest):
    thread_ids: list[UUID] = Field(min_length=1, max_length=100)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class ProjectFileCounts(BaseModel):
    processing: int = 0
    done: int = 0
    failed: int = 0


class ProjectSummary(ProjectResponse):
    folder_count: int
    file_count: int
    file_counts: ProjectFileCounts


class ProjectListResponse(BaseModel):
    items: list[ProjectSummary]


class ProjectFolderResponse(BaseModel):
    id: UUID
    name: str
    status: str
    file_count: int
    attached_at: datetime


class ProjectDetailResponse(ProjectResponse):
    folders: list[ProjectFolderResponse]


class ProjectAttachResponse(BaseModel):
    project_id: UUID
    attached: list[UUID]
    already_attached: list[UUID]


class ProjectDetachResponse(BaseModel):
    project_id: UUID
    detached: list[UUID]


class ProjectDeleteResponse(BaseModel):
    project_id: UUID
    deleted: bool


# ---- Retrieval tools -----------------------------------------------------------------------

class RetrievalFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_types: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    court: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    thread_ids: list[UUID] = Field(default_factory=list)
    doc_ids: list[UUID] = Field(default_factory=list)
    include_images: bool = True

    @model_validator(mode='after')
    def date_order(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError('date_from must not be after date_to.')
        return self


class RetrievalSearchRequest(ProjectIdentityRequest):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    max_per_document: int = Field(default=3, ge=1, le=50)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


class RetrievalDocumentsRequest(ProjectIdentityRequest):
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class RetrievalFactsRequest(ProjectIdentityRequest):
    doc_id: UUID


class RetrievalExpandRequest(ProjectIdentityRequest):
    node_id: UUID
    before: int = Field(default=1, ge=0, le=5)
    after: int = Field(default=1, ge=0, le=5)
    whole_clause: bool = False


class RetrievalScores(BaseModel):
    vector_rank: int | None = None
    keyword_rank: int | None = None
    reference_match: bool | None = None
    rrf: float | None = None
    rerank: float | None = None


class RetrievalPassage(BaseModel):
    kind: Literal['text', 'image']
    id: UUID
    doc_id: UUID
    thread_id: UUID
    file_name: str
    document_type: str | None = None
    title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    heading_path: str = ''
    legal_path: list[str] = Field(default_factory=list)
    node_type: str | None = None
    image_type: str | None = None
    content: str
    model_derived: bool = False
    scores: RetrievalScores = Field(default_factory=RetrievalScores)


class RetrievalTiming(BaseModel):
    embed: float = 0
    candidates: float = 0
    rerank: float = 0
    total: float = 0


class RetrievalSearchResponse(BaseModel):
    query: str
    reranked: bool
    timing_ms: RetrievalTiming
    results: list[RetrievalPassage]


class RetrievalDocument(BaseModel):
    doc_id: UUID
    file_name: str
    file_type: str
    thread_id: UUID
    stages: dict[str, StageState]
    title: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    court: str | None = None
    case_number: str | None = None
    parties: list[dict] = Field(default_factory=list)
    decision_date: date | None = None
    summary: str | None = None


class RetrievalDocumentsResponse(BaseModel):
    items: list[RetrievalDocument]
    limit: int
    offset: int


class RetrievalFactsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    doc_id: UUID
    thread_id: UUID
    title: str | None
    document_type: str | None
    jurisdiction: str | None
    court: str | None
    case_number: str | None
    parties: list[dict]
    judges: list[str]
    decision_date: date | None
    citation: str | None
    statutes_cited: list[str]
    classification: list[str]
    language: str | None
    summary: str | None
    evidence: dict
    details: dict
    model: str | None
    title_embedding: list[float] | None
    created_at: datetime
    updated_at: datetime


class RetrievalExpandResponse(BaseModel):
    node_id: UUID
    doc_id: UUID
    nodes: list[RetrievalPassage]
