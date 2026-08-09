"""교훈 메모리 — 추출·승인·삭제·주입 (`02` 룰 7, `06 §3`, `05 §10`).

> ### 담당자 승인이 유일한 승격 경로다 (룰 7)
> 수정 확정에서 태어난 교훈은 `candidate` 이고, **`approved` 만** 생성 프롬프트의
> `[APPROVED LESSONS]` 에 주입된다. 자동 승인도, 자동 삭제도 없다 — 30개를 넘으면
> 정리를 **제안**할 뿐이고 지우는 것은 담당자의 `DELETE /lessons/{id}` 다.

> ### 삭제는 물리 삭제가 아니다 (D8)
> `status='deleted'` 로 전이시켜 행을 남긴다. 그 행의 `content_hash` 가 후보 재등록을
> 막는 유일한 근거이기 때문이다. 지운 교훈이 다음 수정에서 똑같이 다시 올라오면 담당자는
> 같은 판단을 영원히 반복하게 된다 (`06 §5` 테스트 8).

트랜잭션 경계는 라우터다 — 여기서는 `flush()` 까지만 한다.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.config import settings as env_settings
from app.models.lesson import (
    LESSON_STATUS_APPROVED,
    LESSON_STATUS_CANDIDATE,
    LESSON_STATUS_DELETED,
    Lesson,
)
from app.models.project import Project
from app.models.question import Answer, Question
from app.schemas.lesson import LessonItem, LessonListResponse
from app.services import event_service
from app.services.llm import get_provider
from app.services.llm.base import LLMProviderError
from app.services.pipeline import prompts
from app.services.pipeline.llm_schemas import LessonOut
from app.utils.hashing import lesson_content_hash

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100


def _max_lessons(project: Project) -> int:
    """룰 3 — 임계값은 항상 `projects.settings` 에서 로드한다. 30 을 코드에 박지 않는다."""
    return int(project.settings.get("max_lessons", DEFAULT_SETTINGS["max_lessons"]))


# --------------------------------------------------------------------------------------
# 추출 (수정 확정 시, LLM 1회) — `06 §3`
# --------------------------------------------------------------------------------------
async def extract_candidate(
    db: AsyncSession,
    *,
    project: Project,
    question: Question,
    answer: Answer,
    original_content_en: str | None,
) -> Lesson | None:
    """원답 vs 수정답의 차이에서 교훈 한 줄을 뽑아 후보로 등록한다 (룰 7, `06 §3`).

    돌려주는 값이 `05 §7.1`·`§7.2` 응답의 `lesson_candidate_id` 다. **`None` 이 정상인
    경우가 넷** 있다:

    1. `original_content_en is None` — 카드에 답변 행 자체가 없었다. `reason='failed'` 는
       `answer_id=NULL` 이라(D23) 담당자가 쓴 것이 첫 답변이며, 룰 7 이 교훈을 "원답과
       수정답의 **차이**"로 정의하므로 비교 대상이 없다.
       ⚠️ 빈 문자열(`""`)은 여기 해당하지 않는다 — 강제 🔴 의 초안이 `""` 이고, "시스템은
       답하지 못했는데 담당자는 이렇게 답했다"는 것 자체가 가장 값진 차이다.
    2. 뽑힌 문장이 비어 있다 — 빈 원칙을 프롬프트에 주입할 수는 없다.
    3. 같은 내용의 교훈이 이미 삭제돼 있다 (D8).
    4. **교훈 추출 LLM 호출이 실패했다.**

    ⚠️ 4번이 이 함수가 예외를 밖으로 내보내지 않는 이유다 (룰 9 — "담당자 카드 액션은 **항상
    성공**"). 교훈은 `05 §7.1` 이 "해당 없으면 null" 로 규정한 **부수 산출물**인데, 여기서
    `LLMProviderError` 가 새어 나가면 라우터의 `db.commit()` 에 닿지 못해 번역·확정·공식 Q&A
    편입까지 **통째로 롤백**된다. 교훈 모델이 계속 불안정하면 담당자는 그 답변을 영영 확정할
    수 없게 된다. `LLMSchemaError` 도 `LLMProviderError` 의 하위라 같이 덮인다.

    ⚠️ **이 호출은 `daily_llm_call_limit` 을 소모하지 않는다.** 그 한도의 정의된 실패
    동작은 질문 파이프라인의 강제 🔴 `quota_exceeded` 하나뿐이고(`06 §6`), 담당자 확정
    경로에는 소진 시 동작이 정의돼 있지 않다. 한도로 담당자의 저장을 막으면 룰 9(담당자
    저장이 항상 우선)를 어기게 되므로 `quota.consume` 을 부르지 않는다.
    """
    if original_content_en is None:
        logger.info("원답이 없어 교훈을 추출하지 않는다: answer=%s", answer.id)
        return None

    try:
        result = await get_provider().complete_json(
            prompts.LESSON_SYSTEM,
            prompts.lesson_user_prompt(
                question_en=question.content_en or question.content_ko,
                original_en=original_content_en,
                corrected_en=answer.content_en,
            ),
            LessonOut,
            model=env_settings.llm_model_lesson,
        )
    except LLMProviderError:
        # 룰 9 — 담당자의 확정을 교훈 추출 실패로 되돌리지 않는다. 부수 산출물만 포기한다.
        logger.exception("교훈 추출 LLM 호출이 실패해 후보를 만들지 않는다: answer=%s", answer.id)
        return None

    content = str(result["lesson"]).strip()
    if not content:
        logger.info("빈 교훈이 나와 후보를 만들지 않는다: answer=%s", answer.id)
        return None

    content_hash = lesson_content_hash(content)
    if await _is_deleted(db, project_id=project.id, content_hash=content_hash):
        # D8 — 담당자가 이미 버린 원칙이다. 다시 올리면 같은 판단을 반복시키게 된다.
        logger.info("삭제된 교훈과 같은 내용이라 후보를 만들지 않는다: project=%s", project.id)
        return None

    lesson = Lesson(
        project_id=project.id,
        content=content,
        content_hash=content_hash,
        status=LESSON_STATUS_CANDIDATE,
        needs_recheck=False,
        source_answer_id=answer.id,
        last_used_at=None,
    )
    db.add(lesson)
    await db.flush()

    await event_service.record_event(
        db,
        project_id=project.id,
        type=event_service.EVENT_LESSON_CANDIDATE,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={"lesson_id": str(lesson.id), "answer_id": str(answer.id)},
    )
    return lesson


async def _is_deleted(db: AsyncSession, *, project_id: UUID, content_hash: str) -> bool:
    """같은 프로젝트에 같은 해시의 **삭제된** 교훈이 있는가 (D8).

    프로젝트 스코프로 좁힌다 — 같은 문장이라도 프로젝트가 다르면 다른 원칙이다.
    """
    return (
        await db.scalar(
            select(Lesson.id)
            .where(
                Lesson.project_id == project_id,
                Lesson.content_hash == content_hash,
                Lesson.status == LESSON_STATUS_DELETED,
            )
            .limit(1)
        )
    ) is not None


# --------------------------------------------------------------------------------------
# 승인 · 삭제 — `05 §10`
# --------------------------------------------------------------------------------------
async def approve(db: AsyncSession, *, lesson: Lesson, actor_id: UUID) -> LessonItem:
    """후보 → 승인. **이때부터** 생성 프롬프트에 주입된다 (룰 7).

    ⚠️ **이벤트는 상태가 실제로 바뀔 때만 남긴다** (룰 4 — events 가 지표의 단일 원천).
    `05 §10` 은 이미 승인된 교훈의 재승인에 별도 응답을 규정하지 않았으므로 응답은 그대로
    200 이지만, 이벤트까지 두 번 쌓으면 `05 §13` timeseries 의 `lessons_approved`(버킷별
    승인 교훈 수)가 재호출 횟수만큼 부풀려진다.
    """
    if lesson.status == LESSON_STATUS_APPROVED:
        return to_item(lesson)

    lesson.status = LESSON_STATUS_APPROVED
    await db.flush()

    await event_service.record_event(
        db,
        project_id=lesson.project_id,
        type=event_service.EVENT_LESSON_APPROVED,
        actor_id=actor_id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=await _source_question_id(db, lesson),
        payload={"lesson_id": str(lesson.id)},
    )
    return to_item(lesson)


async def delete(db: AsyncSession, *, lesson: Lesson, actor_id: UUID) -> LessonItem:
    """`status='deleted'` 전이 — **물리 삭제가 아니다** (D8).

    행이 남아야 `content_hash` 대조로 동일 내용 후보 재등록을 막을 수 있다. 삭제된 교훈은
    목록에도 상세 경로에도 나타나지 않는다 (`05 §10` 의 `status` 는 candidate·approved 뿐).
    """
    lesson.status = LESSON_STATUS_DELETED
    await db.flush()

    await event_service.record_event(
        db,
        project_id=lesson.project_id,
        type=event_service.EVENT_LESSON_DELETED,
        actor_id=actor_id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=await _source_question_id(db, lesson),
        payload={"lesson_id": str(lesson.id)},
    )
    return to_item(lesson)


async def _source_question_id(db: AsyncSession, lesson: Lesson) -> UUID | None:
    """이벤트를 붙일 질문 (`05 §13` 타임라인은 질문 스코프로 조회된다).

    `source_answer_id` 는 nullable 이므로(`04 §2`) 없을 수 있다.
    """
    if lesson.source_answer_id is None:
        return None
    return await db.scalar(select(Answer.question_id).where(Answer.id == lesson.source_answer_id))


# --------------------------------------------------------------------------------------
# 문서 갱신 연동 — 룰 5
# --------------------------------------------------------------------------------------
async def flag_needs_recheck(db: AsyncSession, *, answer_ids: list[UUID]) -> int:
    """재검토 연쇄에 걸린 답변에서 나온 교훈에 `needs_recheck=true` 를 붙인다 (룰 5).

    ⛔ **자동 삭제 금지.** 근거가 바뀌었다는 사실과 원칙이 틀렸다는 사실은 다르고, 그
    판단은 담당자 몫이다. 이미 삭제된 교훈은 묘비일 뿐이라 표시 대상이 아니다.
    """
    if not answer_ids:
        return 0

    result = await db.execute(
        update(Lesson)
        .where(
            Lesson.source_answer_id.in_(answer_ids),
            Lesson.status != LESSON_STATUS_DELETED,
            Lesson.needs_recheck.is_(False),
        )
        .values(needs_recheck=True)
    )
    await db.flush()
    return result.rowcount or 0


# --------------------------------------------------------------------------------------
# 주입 — `06 §3`
# --------------------------------------------------------------------------------------
async def load_for_prompt(db: AsyncSession, *, project: Project) -> list[str]:
    """④ 생성 프롬프트에 주입할 승인 교훈 본문 (최신순, 최대 `settings.max_lessons`).

    주입한 교훈의 `last_used_at` 을 갱신한다 — 이 값이 정리 제안(`05 §10`)의 기준이므로,
    갱신을 빠뜨리면 **실제로 쓰이고 있는 교훈이 "안 쓰인 것"으로 제안 목록에 올라간다.**

    ⚠️ `candidate` 는 절대 들어오지 않는다 (룰 7).
    """
    rows = list(
        (
            await db.scalars(
                select(Lesson)
                .where(
                    Lesson.project_id == project.id,
                    Lesson.status == LESSON_STATUS_APPROVED,
                )
                .order_by(Lesson.created_at.desc(), Lesson.id.desc())
                .limit(_max_lessons(project))
            )
        ).all()
    )
    if not rows:
        return []

    # ⚠️ `func.now()` 를 넣지 않는다 — 서버 표현식은 그 속성을 만료시켜 다음 접근이 동기
    #    IO(`MissingGreenlet`)를 부른다 (`official_qa_service.archive` 와 같은 함정).
    #    갱신은 **단일 bulk UPDATE** 다. 행마다 UPDATE 를 내면 `max_lessons` 만큼(기본 30)
    #    왕복이 생기는데, 이 경로는 질문 한 건마다 반드시 지나가는 ④ 생성 직전이다.
    #    `synchronize_session=False` 라 세션 안 인스턴스의 `last_used_at` 은 갱신 전 값으로
    #    남지만, 아래에서 읽는 것은 `content` 뿐이다.
    now = datetime.now(UTC)
    await db.execute(
        update(Lesson)
        .where(Lesson.id.in_([lesson.id for lesson in rows]))
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return [lesson.content for lesson in rows]


# --------------------------------------------------------------------------------------
# 목록 — `05 §10`
# --------------------------------------------------------------------------------------
def _visible_query(project_id: UUID, status: str | None) -> Select[tuple[Lesson]]:
    """삭제된 교훈은 목록에 나타나지 않는다 (`05 §10` — `status` 는 candidate·approved)."""
    stmt = select(Lesson).where(
        Lesson.project_id == project_id,
        Lesson.status != LESSON_STATUS_DELETED,
    )
    if status is not None:
        stmt = stmt.where(Lesson.status == status)
    return stmt


async def list_lessons(
    db: AsyncSession,
    *,
    project: Project,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> LessonListResponse:
    """목록 + 30개 초과 시 정리 제안 (`05 §10`, 룰 7)."""
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    stmt = _visible_query(project.id, status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(
        stmt.order_by(Lesson.created_at.desc(), Lesson.id.desc()).limit(limit).offset(offset)
    )

    return LessonListResponse(
        items=[to_item(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
        cleanup_suggestions=await _cleanup_suggestions(db, project=project),
    )


async def _cleanup_suggestions(db: AsyncSession, *, project: Project) -> list[UUID]:
    """정리 **제안** — 승인 교훈이 `max_lessons` 를 넘을 때만, 초과분만큼 (룰 7).

    ⛔ **자동 삭제는 없다.** 넘친 만큼의 id 를 돌려줄 뿐이고 지우는 것은 담당자다.

    순서는 "오래되고 쓰이지 않은 것부터"다: `last_used_at` 이 NULL(승인 후 한 번도 주입되지
    않았다) → 마지막 사용이 오래된 것 → 동률이면 먼저 만들어진 것.
    """
    max_lessons = _max_lessons(project)
    approved = (
        await db.scalar(
            select(func.count())
            .select_from(Lesson)
            .where(
                Lesson.project_id == project.id,
                Lesson.status == LESSON_STATUS_APPROVED,
            )
        )
        or 0
    )
    if approved <= max_lessons:
        return []

    rows = await db.scalars(
        select(Lesson.id)
        .where(
            Lesson.project_id == project.id,
            Lesson.status == LESSON_STATUS_APPROVED,
        )
        .order_by(Lesson.last_used_at.asc().nulls_first(), Lesson.created_at.asc())
        .limit(approved - max_lessons)
    )
    return list(rows.all())


def to_item(lesson: Lesson) -> LessonItem:
    return LessonItem(
        id=lesson.id,
        content=lesson.content,
        status=lesson.status,  # type: ignore[arg-type]
        needs_recheck=lesson.needs_recheck,
        last_used_at=lesson.last_used_at,
        source_answer_id=lesson.source_answer_id,
        created_at=lesson.created_at,
    )
