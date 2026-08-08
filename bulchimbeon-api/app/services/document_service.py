"""문서 도메인 서비스 (`05 §4`, `02 §5`, D9·D20).

커밋은 라우터가 한다 (요청 하나 = 트랜잭션 하나). 여기서는 `flush()` 까지만 한다.
"""

import asyncio
import logging
import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MAX_DOCUMENTS_PER_PROJECT, MAX_UPLOAD_BYTES, settings
from app.core.errors import (
    NotFound,
    PipelineFailed,
    PipelineInProgress,
    UnsupportedFileType,
    ValidationError,
)
from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    DOCUMENT_STATUS_DELETED,
    INGEST_STATUS_FAILED,
    INGEST_STATUS_READY,
    SOURCE_TYPE_UPLOAD,
    Document,
    DocumentVersion,
)
from app.models.user import User
from app.schemas.document import DocumentDetail, DocumentOut, DocumentVersionOut
from app.services import review_cascade_service
from app.services.event_service import EVENT_DOCUMENT_VERSION_ACTIVATED, record_event
from app.utils.parsing import EXTENSION_TO_MIME, DocumentParseError, parse_file
from app.utils.storage import resolve_storage_path

logger = logging.getLogger(__name__)

# 업로드 본문을 1MB 씩 읽어 상한을 넘기는 즉시 끊는다 — 20MB 상한을 두고 1GB 를
# 통째로 메모리에 올리면 상한이 방어가 아니라 표시가 된다.
_READ_CHUNK_BYTES = 1024 * 1024

# 파일명은 사용자 입력이다. 경로 성분(`../`)을 제거하고 화이트리스트로 정규화한다 (`03 §7`).
_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.\- ]", flags=re.UNICODE)
_MAX_FILENAME_STEM = 100

# `05 §1.5` — 서버가 만드는 사용자 표시 문자열은 **수신자 `users.language`** 로 만든다.
_ACTIVATE_MESSAGES: dict[str, dict[str, str]] = {
    "ko": {
        "none": "재검토 대상 확정 답변이 없습니다.",
        "some": "이 문서를 근거로 한 확정 답변 {count}건이 재검토 대상입니다.",
    },
    "en": {
        "none": "No verified answers need review.",
        "some": "{count} verified answers based on this document are up for review.",
    },
}


# --- 조회 -------------------------------------------------------------------------------


async def load_document(db: AsyncSession, document_id: UUID) -> Document:
    document = await db.get(Document, document_id)
    if document is None:
        raise NotFound()
    return document


async def load_writable_document(db: AsyncSession, document_id: UUID) -> Document:
    """쓰기 대상 문서.

    soft delete 된 문서에는 새 버전을 올리거나 버전을 활성화할 수 없다 — 되살리기는
    MVP 범위 밖이고(D19 와 같은 취지), 삭제된 문서의 청크를 다시 검색 대상으로 올리는 것은
    D20 이 금지한 상태다. 읽기(상세·원문 열람)는 그대로 열어 둔다 — 과거 답변의
    `citations[]` 가 가리키는 원문은 계속 열려야 한다 (`05 §6`).
    """
    document = await load_document(db, document_id)
    if document.status == DOCUMENT_STATUS_DELETED:
        raise NotFound("삭제된 문서입니다.")
    return document


async def load_version(db: AsyncSession, document: Document, version_id: UUID) -> DocumentVersion:
    version = await db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document.id:
        raise NotFound()
    return version


async def _versions_of(db: AsyncSession, document_id: UUID) -> list[DocumentVersion]:
    return list(
        (
            await db.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.version_no.desc())
            )
        ).all()
    )


def _to_version_out(version: DocumentVersion) -> DocumentVersionOut:
    return DocumentVersionOut(
        id=version.id,
        version_no=version.version_no,
        original_filename=version.original_filename,
        mime=version.mime,
        is_active=version.is_active,
        ingest_status=version.ingest_status,
        ingest_error=version.ingest_error,
        uploaded_by=version.uploaded_by,
        created_at=version.created_at,
    )


def _to_document_out(document: Document, versions: list[DocumentVersion]) -> DocumentOut:
    active = next((version for version in versions if version.is_active), None)
    return DocumentOut(
        id=document.id,
        title=document.title,
        source_type=document.source_type,
        source_ref=document.source_ref,
        status=document.status,
        created_at=document.created_at,
        updated_at=document.updated_at,
        active_version=_to_version_out(active) if active is not None else None,
    )


async def to_detail(db: AsyncSession, document: Document) -> DocumentDetail:
    versions = await _versions_of(db, document.id)
    base = _to_document_out(document, versions)
    return DocumentDetail(
        **base.model_dump(), versions=[_to_version_out(version) for version in versions]
    )


async def list_documents(db: AsyncSession, project_id: UUID) -> list[DocumentOut]:
    """`05 §4` 목록 — 활성 버전 요약·ingest_status 포함.

    soft delete 된 문서는 빠진다 (D20).
    """
    documents = list(
        (
            await db.scalars(
                select(Document)
                .where(
                    Document.project_id == project_id,
                    Document.status == DOCUMENT_STATUS_ACTIVE,
                )
                .order_by(Document.created_at.desc())
            )
        ).all()
    )
    if not documents:
        return []

    versions = list(
        (
            await db.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id.in_([document.id for document in documents]))
                .order_by(DocumentVersion.version_no.desc())
            )
        ).all()
    )
    by_document: dict[UUID, list[DocumentVersion]] = {}
    for version in versions:
        by_document.setdefault(version.document_id, []).append(version)

    return [_to_document_out(document, by_document.get(document.id, [])) for document in documents]


# --- 업로드 -----------------------------------------------------------------------------


def _resolve_mime(filename: str | None) -> tuple[str, str]:
    """확장자 화이트리스트 (`05 §4`). 위반은 400 `UNSUPPORTED_FILE_TYPE`.

    mime 은 **확장자에서 결정한다** — 클라이언트가 보내는 `Content-Type` 은 브라우저·OS 마다
    달라(`application/octet-stream` 등) 파서 선택의 기준으로 쓸 수 없다.
    """
    suffix = Path(filename or "").suffix.lower()
    mime = EXTENSION_TO_MIME.get(suffix)
    if mime is None:
        raise UnsupportedFileType(
            f"지원하지 않는 파일 형식입니다. 허용: {', '.join(sorted(EXTENSION_TO_MIME))}"
        )
    return suffix, mime


def _safe_filename(filename: str | None, suffix: str) -> str:
    stem = Path(filename or "").stem
    stem = _UNSAFE_FILENAME_CHARS.sub("_", stem).strip(" ._")[:_MAX_FILENAME_STEM]
    return f"{stem or 'document'}{suffix}"


async def _read_capped(file: UploadFile) -> bytes:
    """상한을 넘기는 즉시 끊는다 (`03 §7` — 파일당 20MB)."""
    parts: list[bytes] = []
    total = 0
    while True:
        part = await file.read(_READ_CHUNK_BYTES)
        if not part:
            break
        total += len(part)
        if total > MAX_UPLOAD_BYTES:
            raise ValidationError(
                f"파일이 너무 큽니다. 최대 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 입니다."
            )
        parts.append(part)

    if total == 0:
        raise ValidationError("빈 파일은 업로드할 수 없습니다.")
    return b"".join(parts)


async def _store(project_id: UUID, version_id: UUID, filename: str, payload: bytes) -> str:
    """`STORAGE_DIR/{project_id}/{version_id}/{filename}` 에 저장하고 **상대 경로**를 돌려준다.

    디스크 쓰기는 블로킹이므로 스레드로 밀어낸다 (룰 8).
    """
    relative = Path(str(project_id)) / str(version_id) / filename
    target = settings.storage_dir / relative

    def _write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    await asyncio.get_running_loop().run_in_executor(None, _write)
    return str(relative)


async def _next_version_no(db: AsyncSession, document_id: UUID) -> int:
    """다음 버전 순번.

    ⚠️ 호출 전에 반드시 상위 `documents` 행을 `FOR UPDATE` 로 잠근다 —
    `max(version_no) + 1` 은 read-modify-write 라서, 같은 문서에 동시에 재업로드가 들어오면
    두 요청이 같은 번호를 계산해 `UNIQUE(document_id, version_no)` 로 500 이 된다.
    잠금 위치는 `activate_version` 과 같아 두 경로가 하나의 순서로 직렬화된다.
    """
    current = await db.scalar(
        select(func.max(DocumentVersion.version_no)).where(
            DocumentVersion.document_id == document_id
        )
    )
    return (current or 0) + 1


async def create_version(
    db: AsyncSession,
    *,
    document: Document,
    uploader: User,
    file: UploadFile,
    auto_activate: bool,
) -> DocumentVersion:
    """새 버전을 만든다. 재업로드 = 새 버전이며 **기존 버전은 지우지 않는다** (룰 5).

    ⚠️ **여기서는 활성화하지 않는다.** `auto_activate=True` 는 "지금 활성화"가 아니라
    "**인제스트가 `ready` 에 도달하면** 자동 활성화"를 뜻한다 (`02 §5` 구현 노트).
    업로드 시점에 활성화해 버리면 인제스트가 실패했을 때
    신버전은 `is_active=true, failed`, 구버전은 `is_active=false, ready` 가 되어
    **양쪽 다 검색 범위에서 탈락**한다 — 멀쩡하던 구버전 근거까지 함께 사라진다.
    활성 전환은 `services/pipeline/ingest.py` 가 ready 커밋 직후에 수행한다.

    `auto_activate=False` 면 인제스트가 성공해도 활성화하지 않는다. 재검토 연쇄 역시
    담당자가 명시적으로 활성 전환할 때까지 미뤄진다 (`02 §5` 구현 노트).
    """
    suffix, mime = _resolve_mime(file.filename)
    payload = await _read_capped(file)

    version_id = uuid4()
    filename = _safe_filename(file.filename, suffix)
    storage_path = await _store(document.project_id, version_id, filename, payload)

    # 상위 행 잠금 — 동시 재업로드가 같은 `version_no` 를 계산하는 것을 막는다.
    await db.execute(select(Document.id).where(Document.id == document.id).with_for_update())

    version = DocumentVersion(
        id=version_id,
        document_id=document.id,
        version_no=await _next_version_no(db, document.id),
        original_filename=filename,
        mime=mime,
        storage_path=storage_path,
        is_active=False,  # 활성화는 반드시 activate_version 의 3문 절차를 거친다 (`04 §7`).
        uploaded_by=uploader.id,
    )
    db.add(version)
    await db.flush()
    # `created_at`·`ingest_status` 는 server_default 다 — 응답 스키마가 바로 읽으므로
    # 값이 채워진 상태로 만들어 둔다.
    await db.refresh(version)
    return version


async def create_document(
    db: AsyncSession,
    *,
    project_id: UUID,
    uploader: User,
    file: UploadFile,
    title: str | None,
    auto_activate: bool,
) -> tuple[Document, DocumentVersion]:
    """업로드 → 문서 + 버전 1 (`05 §4`). 인제스트는 라우터가 백그라운드로 띄운다."""
    active_documents = await db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.project_id == project_id, Document.status == DOCUMENT_STATUS_ACTIVE)
    )
    if (active_documents or 0) >= MAX_DOCUMENTS_PER_PROJECT:
        raise ValidationError(
            f"프로젝트당 문서는 최대 {MAX_DOCUMENTS_PER_PROJECT}개입니다 (`03 §7`)."
        )

    document = Document(
        project_id=project_id,
        title=(title or Path(file.filename or "document").stem).strip() or "document",
        source_type=SOURCE_TYPE_UPLOAD,
        source_ref=None,
        status=DOCUMENT_STATUS_ACTIVE,
    )
    db.add(document)
    await db.flush()
    await db.refresh(document)

    version = await create_version(
        db, document=document, uploader=uploader, file=file, auto_activate=auto_activate
    )
    return document, version


# --- 활성 전환 --------------------------------------------------------------------------


def ensure_activatable(version: DocumentVersion) -> None:
    """`ready` 가 아닌 버전은 활성화하지 않는다.

    활성인데 검색되지 않는 버전은 **근거 공백**을 만든다 — 구버전이 내려간 자리를
    아무도 채우지 못한다. 그래서 자동 경로(인제스트 완료)든 수동 경로(`PATCH .../activate`)든
    같은 관문을 지난다. 코드는 계약서 `05 §1.4` 표 안에서만 고른다.
    """
    if version.ingest_status == INGEST_STATUS_READY:
        return
    if version.ingest_status == INGEST_STATUS_FAILED:
        raise PipelineFailed("인제스트에 실패한 버전은 활성화할 수 없습니다.")
    raise PipelineInProgress("인제스트가 끝난 뒤에 활성화할 수 있습니다.")


async def activate_version(
    db: AsyncSession, *, document: Document, version: DocumentVersion, actor_id: UUID
) -> int:
    """활성 버전 전환 — **부분 UNIQUE 3문 스왑** (`04 §7`, 프롬프트 5번).

    ⚠️ 단일 `UPDATE … CASE` 로 스왑하지 않는다. 부분 UNIQUE 인덱스는 `DEFERRABLE` 이 아니라
    문장 중간 상태에서 제약을 위반한다. M-1 실측(2026-08-07, 로컬 pg16.14 · 로컬 pg18.4 ·
    Railway pg18.4 동일)에서 단일 UPDATE 는 **행 처리 순서에 따라 통과하기도 했다** —
    낮은→높은 방향만 시험하면 초록으로 통과한 뒤 운영에서 간헐 `23505` 가 난다.

    `actor_id` 는 `User` 가 아니라 UUID 다 — 인제스트 백그라운드 태스크도 이 함수를 부르는데,
    그쪽에는 ORM 객체가 없다 (`03 §2` 원칙 4).

    돌려주는 값은 재검토 연쇄 건수다 (`05 §4` `review_cascade_count`).
    """
    ensure_activatable(version)

    # ① 상위 행 잠금 — 동시 활성 전환 요청을 직렬화한다.
    await db.execute(select(Document.id).where(Document.id == document.id).with_for_update())

    # ⚠️ `synchronize_session="fetch"` 는 성능 취향이 아니라 정합성 요구다 (M1 에서 실측).
    #    이 요청은 이미 버전 객체를 세션에 올려 둔 상태다. `False` 로 두면 낡은 `is_active` 가
    #    identity map 에 남아, 직후 조회가 DB 가 아니라 그 객체를 돌려준다.

    # ② 전부 내린다
    await db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .values(is_active=False)
        .execution_options(synchronize_session="fetch")
    )

    # ③ 하나만 올린다
    await db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version.id)
        .values(is_active=True)
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()

    # 재검토 연쇄 (룰 5, D9) — 이 문서의 **다른 버전**을 근거로 확정된 답변(과 그 공식 Q&A)을
    # 전부 under_review 로 내리고 `doc_update` 카드 묶음을 만든다. 묶음 키는 새 버전 id 다.
    review_cascade_count = await review_cascade_service.cascade_for_document(
        db,
        document=document,
        trigger_version_id=version.id,
        exclude_version_id=version.id,
        actor_id=actor_id,
    )

    await record_event(
        db,
        project_id=document.project_id,
        type=EVENT_DOCUMENT_VERSION_ACTIVATED,
        actor_id=actor_id,
        entity_type="document_version",
        entity_id=version.id,
        payload={
            "document_id": str(document.id),
            "version_no": version.version_no,
            "review_cascade_count": review_cascade_count,
        },
    )
    return review_cascade_count


def activation_message(language: str, count: int) -> str:
    """`05 §4` 활성 전환 응답의 `message` — 수신자 언어로 만든다 (`05 §1.5`)."""
    table = _ACTIVATE_MESSAGES.get(language, _ACTIVATE_MESSAGES["ko"])
    if count == 0:
        return table["none"]
    return table["some"].format(count=count)


# --- 삭제 · 원문 열람 -------------------------------------------------------------------


async def soft_delete(db: AsyncSession, document: Document, *, actor_id: UUID) -> int:
    """soft delete (D20). 돌려주는 값은 재검토 연쇄 건수다.

    청크는 **물리 삭제하지 않는다** — 과거 답변의 `citations[]` 가 청크를 가리키고 있다.
    검색에서 빠지는 것은 `pipeline/retrieval.py` 의 범위 조건이 담당한다.

    근거가 **바뀌는 것**과 **사라지는 것**은 답변 입장에서 동일한 사건이므로 활성 전환과
    **같은 연쇄**를 태운다. 다만 파생된 공식 Q&A 는 `under_review` 가 아니라 `archived` 다 —
    돌아올 근거 문서가 없다.
    """
    if document.status == DOCUMENT_STATUS_DELETED:
        return 0  # 재호출은 멱등이다.

    # 카드 묶음 키로 쓸 "사라지는 시점의 활성 버전". 이 값이 있어야 담당자가 bulk-keep 으로
    # 한 번에 유지할 수 있다 (`05 §7`).
    active_version_id = await db.scalar(
        select(DocumentVersion.id).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.is_active.is_(True),
        )
    )

    document.status = DOCUMENT_STATUS_DELETED
    await db.flush()

    return await review_cascade_service.cascade_for_document(
        db,
        document=document,
        trigger_version_id=active_version_id,
        archive_official_qas=True,
        actor_id=actor_id,
    )


async def read_content(db: AsyncSession, version: DocumentVersion) -> str:
    """근거 원문 열람 (기능 2.2, `05 §4`).

    파싱 결과를 컬럼에 저장하지 않으므로(`04 §2` 에 그런 컬럼이 없다) 저장 파일을 그때그때
    다시 파싱한다. 청크를 이어 붙이지 않는 이유는 오버랩 때문에 원문이 중복되기 때문이다.

    ⚠️ mime 은 확장자로 정하므로 내용이 깨진 `.pdf` 도 업로드는 201 로 통과한다(인제스트는
    failed). 그 버전의 열람 URL 을 치면 파싱이 다시 실패하는데, `DocumentParseError` 는
    `AppError` 가 아니라 그대로 두면 전역 핸들러가 **500** 으로 떨어뜨린다.
    계약서에 이미 있는 422 `PIPELINE_FAILED` 로 변환한다 (`05 §1.4` — 코드 신설 없음).
    """
    path = resolve_storage_path(version.storage_path)

    def _work() -> str:
        return "\n\n".join(page.text for page in parse_file(path, version.mime) if page.text)

    if not path.exists():
        raise NotFound("원문 파일을 찾을 수 없습니다.")

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _work)
    except DocumentParseError as exc:
        logger.warning("원문 파싱 실패: version=%s (%s)", version.id, exc)
        raise PipelineFailed("원문을 읽을 수 없는 문서입니다.") from exc
