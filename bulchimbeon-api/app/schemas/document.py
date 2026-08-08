"""문서 스키마 (`05 §4` 와 1:1).

계약서 §4 는 표와 산문으로 필드를 규정한다:
- 목록: "활성 버전 요약·ingest_status 포함"
- 상세: "문서 + 버전 목록"
- 활성 전환: `{document_id, active_version, review_cascade_count, message}` (예시 JSON)
- 원문 열람: `{document_id, version_id, version_no, title, mime, content}` (§4 산문)

여기 있는 필드는 전부 위 규정 또는 `04 §2` 의 컬럼에서 온 것이다.
계약서에 없는 필드를 새로 만들지 않는다.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentVersionOut(BaseModel):
    """`04 §2` document_versions 의 표시용 사영. `storage_path` 는 내려보내지 않는다(내부 경로)."""

    id: UUID
    version_no: int
    original_filename: str
    mime: str
    is_active: bool
    ingest_status: str
    ingest_error: str | None
    uploaded_by: UUID
    created_at: datetime


class DocumentOut(BaseModel):
    """목록 아이템 — 활성 버전 요약을 함께 싣는다 (`05 §4`)."""

    id: UUID
    title: str
    source_type: str
    source_ref: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    active_version: DocumentVersionOut | None


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]


class DocumentDetail(DocumentOut):
    """상세 — 문서 + **버전 목록** (`05 §4`). 업로드·새 버전 응답(201)도 같은 shape 이다."""

    versions: list[DocumentVersionOut]


class ActivateVersionResponse(BaseModel):
    """`05 §4` PATCH .../activate 200 예시와 1:1.

    `active_version` 은 **버전 순번(int)** 이다 — 예시가 `2` 를 준다. id 가 아니다.
    """

    document_id: UUID
    active_version: int
    review_cascade_count: int
    message: str


class DocumentContentResponse(BaseModel):
    """`05 §4` — 근거 원문 열람 (기능 2.2).

    §6 `citations[]` 의 `document_id`·`document_version_id` 로 구성되는 URL 의 응답이다.
    """

    document_id: UUID
    version_id: UUID
    version_no: int
    title: str
    mime: str
    content: str


# multipart 의 파일 외 필드(`title`·`auto_activate`)는 라우터에서 `Form(...)` 으로 직접 받는다.
# Pydantic 모델로 감싸면 FastAPI 가 그것을 **본문 스키마**로 해석해 multipart 계약이 깨진다.
# `title` 의 길이 제한은 `routers/documents.py` 의 `Form(max_length=...)` 이 강제한다.
MAX_TITLE_LENGTH = 200
