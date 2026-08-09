"""질문 파이프라인 (`06 §2` ①~⑧) — 번역 → 재사용 → 검색 → 생성 → 근거검증 → 등급 → 발행.

> ### ⚠️ BackgroundTasks 규약 (`03 §2` 원칙 4 — 위반하면 질문이 `processing` 에 **영구 정지**한다)
> - 진입점 `run_answer_pipeline` 은 **`question_id: UUID` 하나만** 받는다. ORM 객체·
>   `AsyncSession` 금지 — FastAPI 0.106.0 부터 `yield` 의존성이 백그라운드 태스크보다
>   **먼저** 정리되므로 요청 세션은 태스크 실행 시점에 이미 닫혀 있다.
> - 태스크 안에서 자체 세션을 연다. **실패를 기록할 때는 또 다른 새 세션**을 쓴다 —
>   예외가 난 세션은 이미 롤백 대상이라 그 위에서 커밋할 수 없다.

> ### 이 파일이 절대 놓치면 안 되는 것
> - `sim_raw` 는 `1 - (embedding <=> :q)` 다. `<=>` 는 **거리**다 (`retrieval.py` 가 유일한 산출점).
> - **G 의 분모는 생성 시점 원본 문장 수로 고정**이다. 프루닝은 매칭률을 올리지 못한다.
> - **강제 🔴 4종은 DND 에서도 🔴 을 유지**한다. 강등 대상은 `low_confidence` 뿐이다 (D2).
> - 재사용 답변은 **확정 당시 한국어 원문 그대로** 나간다. 재번역 금지 (룰 4·D5).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.config import settings as env_settings
from app.database import AsyncSessionLocal
from app.models.official_qa import OfficialQA
from app.models.project import Project
from app.models.question import (
    ANSWER_SOURCE_GENERATED,
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_VERIFIED,
    GRADE_GREEN,
    GRADE_RED,
    GRADE_YELLOW,
    HELD_REASON_CONFLICT,
    HELD_REASON_LOW_CONFIDENCE,
    HELD_REASON_NO_EVIDENCE,
    HELD_REASON_QUOTA_EXCEEDED,
    HELD_REASON_SCHEMA_FAILED,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_FAILED,
    QUESTION_STATUS_HELD,
    QUESTION_STATUS_PROCESSING,
    Answer,
    AnswerCitation,
    Question,
)
from app.models.review_card import CARD_REASON_FAILED
from app.models.user import User
from app.services import (
    event_service,
    lesson_service,
    notification_service,
    project_service,
    review_card_service,
    sse_manager,
)
from app.services.llm import LLMSchemaError, get_provider
from app.services.pipeline import dnd, grading, prompts, quota, retrieval
from app.services.pipeline.llm_schemas import (
    QuestionStructOut,
    SameQuestionOut,
    SentencesOut,
    TranslationOut,
    VerdictsOut,
)
from app.services.pipeline.retrieval import EvidenceChunk

logger = logging.getLogger(__name__)


def _default_session_factory() -> AsyncSession:
    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 **유일한 지점**이다 (`ingest.session_factory` 와 같은 이유).
#    테스트 세션은 바깥 트랜잭션에 물린 커넥션을 공유해야 롤백 격리가 성립한다 (`03 §5.3`).
session_factory: Callable[[], AsyncSession] = _default_session_factory


class PipelineDeadlineExceeded(RuntimeError):
    """데드라인을 넘겼는데 **발행할 결과가 하나도 없다** → 총 실패 (D23, `06 §6`)."""


@dataclass(frozen=True)
class _Outcome:
    """세션이 닫힌 뒤에 쓰이므로 ORM 객체를 들고 다니지 않는다."""

    project_id: UUID
    question_id: UUID
    grade: str | None
    status: str


@dataclass
class _Sentence:
    """④ 결과 1문장 + 서버가 붙인 판정."""

    index: int  # 생성 시점 1-based 순번. **G 의 분모는 이 순번의 총 개수다.**
    text_en: str
    text_ko: str
    aliases: list[str]  # EVIDENCE 에 실재하는 별칭만 남은 것
    supported: bool = False


@dataclass
class _Ctx:
    db: AsyncSession
    question: Question
    project: Project
    answerer: User
    started: float
    steps: list[tuple[str, int]] = field(default_factory=list)

    def setting(self, key: str) -> Any:
        """임계값은 **`projects.settings` 에서 로드**한다 (룰 3).

        키가 없을 때만 `DEFAULT_SETTINGS` 로 떨어진다 — 기본값의 정의 위치는 거기 한 곳이다.
        """
        return self.project.settings.get(key, DEFAULT_SETTINGS[key])

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def over_deadline(self, *, red_path: bool = False) -> bool:
        """🟢/🟡 경로 25초 · 🔴 경로 35초 (`06 §0` M-1 실측 p90 기준).

        단일 25초로는 🔴 경로(호출 4회)가 성립하지 않아 등급별로 분리돼 있다.
        """
        budget = (
            env_settings.llm_pipeline_deadline_red_seconds
            if red_path
            else env_settings.llm_pipeline_deadline_seconds
        )
        return self.elapsed_ms() > budget * 1000

    async def call_json(
        self, *, step: str, system: str, user: str, schema: type, model: str
    ) -> dict[str, Any]:
        """LLM 1회 + 단계별 elapsed 로깅 + 일일 호출 카운트 (`06 §6`)."""
        quota.consume(self.project.id)
        before = self.elapsed_ms()
        result = await get_provider().complete_json(system, user, schema, model=model)
        self.steps.append((step, self.elapsed_ms() - before))
        logger.info(
            "pipeline step=%s question=%s elapsed_ms=%s",
            step,
            self.question.id,
            self.elapsed_ms(),
        )
        return result


# --------------------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------------------
async def run_answer_pipeline(question_id: UUID) -> None:
    """백그라운드 진입점. **UUID 하나만** 받는다 (`03 §2` 원칙 4).

    ⚠️ 알림 레코드와 SSE 는 파이프라인 **안에서** 적재된다. SSE 발행은 커밋 직후
    `sse_manager` 의 아웃박스가 하므로 여기서 따로 부르지 않는다 — 커밋 전에 발행하면
    프론트의 재조회가 커밋 전 상태를 읽는다 (`sse_manager` 독스트링).
    """
    outcome: _Outcome | None = None

    try:
        async with session_factory() as db:  # 태스크 자체 세션
            outcome = await _pipeline(db, question_id)
            await db.commit()
    except Exception as exc:
        logger.exception("answer pipeline 실패: question=%s", question_id)
        outcome = None  # 커밋되지 않은 결과를 완료로 기록하지 않는다.
        try:
            async with session_factory() as db:  # ⚠️ 실패 기록은 반드시 **새 세션**
                outcome = await mark_question_failed(db, question_id, exc)
                await db.commit()
        except Exception:
            logger.exception("answer pipeline 실패 기록마저 실패: question=%s", question_id)

    if outcome is not None:
        logger.info(
            "pipeline 완료: question=%s grade=%s status=%s",
            outcome.question_id,
            outcome.grade,
            outcome.status,
        )


async def mark_question_failed(
    db: AsyncSession, question_id: UUID, exc: Exception
) -> _Outcome | None:
    """파이프라인 총 실패 (D23). **질문은 어떤 경우에도 사라지지 않는다.**"""
    question = await db.get(Question, question_id)
    if question is None:
        logger.warning("실패를 기록할 질문이 없다: %s", question_id)
        return None
    if question.status != QUESTION_STATUS_PROCESSING:
        return None

    logger.error("question=%s 파이프라인 총 실패: %s", question_id, exc)
    previous = question.status
    question.status = QUESTION_STATUS_FAILED

    await event_service.record_event(
        db,
        project_id=question.project_id,
        type=event_service.EVENT_QUESTION_STATUS_CHANGED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={"from": previous, "to": QUESTION_STATUS_FAILED},
    )
    # 실패 안전망 카드 (D23) — **카드가 없으면 담당자가 이 질문을 볼 길이 없다.**
    # 재처리(파이프라인 재실행) API 가 MVP 에 없으므로(`04 §6.1`) 해소 경로는 이 카드뿐이다.
    card = await review_card_service.create_card(
        db, question=question, answer=None, reason=CARD_REASON_FAILED
    )

    # 질문자에게 `answer.failed` (D23) + 담당자에게 `card.created` (룰 6).
    await notification_service.notify_answer_failed(db, question=question)
    project = await db.get(Project, question.project_id)
    if project is not None and review_card_service.notifies_answerer(card):
        await notification_service.notify_card_created(
            db, card=card, question=question, project=project
        )

    # 🔴·실패에도 발행한다 — 알리지 않으면 프론트가 `processing` 으로 영원히 폴링한다.
    sse_manager.queue_answer_completed(
        db,
        asker_id=question.asker_id,
        question_id=question.id,
        grade=None,
        status=QUESTION_STATUS_FAILED,
    )
    await db.flush()
    return _Outcome(
        project_id=question.project_id,
        question_id=question.id,
        grade=None,
        status=QUESTION_STATUS_FAILED,
    )


# --------------------------------------------------------------------------------------
# 본체
# --------------------------------------------------------------------------------------
async def _pipeline(db: AsyncSession, question_id: UUID) -> _Outcome | None:
    question = await db.get(Question, question_id)
    if question is None:
        logger.warning("파이프라인 대상 질문이 없다: %s", question_id)
        return None
    if question.status != QUESTION_STATUS_PROCESSING:
        # 이미 처리된 질문. `answers` 의 UNIQUE(question_id) 와 함께 재실행을 막는다.
        logger.info("이미 처리된 질문이다: %s (status=%s)", question_id, question.status)
        return None

    project = await db.get(Project, question.project_id)
    if project is None:
        raise RuntimeError(f"질문의 프로젝트가 없다: {question.project_id}")

    # DND·브리핑 시각 판정의 단일 원천은 **현재 담당자의 users.timezone** 이다 (`04 §3`).
    answerer = await db.get(User, project.answerer_id)
    if answerer is None:
        raise RuntimeError(f"프로젝트 담당자가 없다: {project.answerer_id}")

    ctx = _Ctx(
        db=db, question=question, project=project, answerer=answerer, started=time.monotonic()
    )

    # --- 일일 호출 상한 (`06 §6`) — LLM 을 한 번도 부르기 전에 본다 -----------------------
    if quota.is_exceeded(project.id, ctx.setting("daily_llm_call_limit")):
        logger.warning("daily_llm_call_limit 초과: project=%s", project.id)
        return await _publish_generated(
            ctx,
            grade=GRADE_RED,
            held_reason=HELD_REASON_QUOTA_EXCEEDED,
            published=[],
            original_count=0,
            removed=[],
            evidence=[],
            sim_raw=None,
            search_score=None,
            g_raw=None,
            g_final=None,
            similar_official_qa_id=None,
            # ⑦ 구조화도 LLM 호출이다 — 한도를 넘긴 상태에서 부를 수 없다.
            skip_struct=True,
        )

    # --- ① 번역 + 긴급 제안 -----------------------------------------------------------
    _guard_deadline(ctx, "translate")
    translation = await ctx.call_json(
        step="translate",
        system=prompts.TRANSLATE_SYSTEM,
        user=question.content_ko,  # ⚠️ 장식 금지 (prompts 독스트링)
        schema=TranslationOut,
        model=env_settings.llm_model_translate,
    )
    question.content_en = translation["content_en"]
    question.suggest_urgent = bool(translation["suggest_urgent"])
    await db.flush()

    # --- ② 재사용 검사 ---------------------------------------------------------------
    # 임베딩은 **1회**다. 여기서 만든 벡터를 ③ 이 그대로 재사용한다 (`06 §0`).
    query_embedding = (await get_provider().embed([question.content_en]))[0]

    reuse_threshold = float(ctx.setting("reuse_threshold"))
    similar_threshold = float(ctx.setting("similar_threshold"))
    similar_official_qa_id: UUID | None = None

    candidate = await retrieval.search_official_qa(
        db, project_id=project.id, query_embedding=query_embedding
    )
    if candidate is not None:
        official_qa, best_similarity = candidate

        if best_similarity >= reuse_threshold:
            # 1차 통과는 **후보일 뿐**이다. 단일 임계값에 재사용을 걸지 않는다 (룰 4).
            _guard_deadline(ctx, "reuse_gate")
            gate = await ctx.call_json(
                step="reuse_gate",
                system=prompts.SAME_QUESTION_SYSTEM,
                user=(
                    f"[QUESTION A] {official_qa.question_en}\n[QUESTION B] {question.content_en}"
                ),
                schema=SameQuestionOut,
                model=env_settings.llm_model_reuse_gate,
            )
            if gate["same_question"]:
                return await _publish_reused(ctx, official_qa, best_similarity)
            await _record_reuse_missed(ctx, best_similarity)

        elif best_similarity >= similar_threshold:
            # 계속 진행하되 "비슷한 확정 답변"을 첨부한다. **공식 Q&A 노출 경로는 이것뿐** (D24).
            similar_official_qa_id = official_qa.id
            await _record_reuse_missed(ctx, best_similarity)

    # --- ③ 근거 검색 -----------------------------------------------------------------
    _guard_deadline(ctx, "retrieval")
    evidence = await retrieval.search_evidence(
        db,
        project_id=project.id,
        query_embedding=query_embedding,
        top_k=int(ctx.setting("retrieval_top_k")),
    )
    if not evidence:
        return await _forced_red(
            ctx,
            HELD_REASON_NO_EVIDENCE,
            evidence=[],
            sim_raw=None,
            search_score=None,
            similar_official_qa_id=similar_official_qa_id,
        )

    sim_raw = evidence[0].sim_raw
    search_score = grading.search_score(
        sim_raw, s_floor=float(ctx.setting("s_floor")), s_ceil=float(ctx.setting("s_ceil"))
    )

    if sim_raw < float(ctx.setting("similarity_floor")):
        # 벡터 검색은 항상 "가장 가까운 무언가"를 돌려준다. 하한이 없으면 무관한 청크로
        # 답을 만들게 된다 (`06 §2` ③).
        return await _forced_red(
            ctx,
            HELD_REASON_NO_EVIDENCE,
            evidence=evidence,
            sim_raw=sim_raw,
            search_score=search_score,
            similar_official_qa_id=similar_official_qa_id,
        )

    # --- ④ 인용 강제 생성 -------------------------------------------------------------
    aliases = {f"ch-{index}": chunk for index, chunk in enumerate(evidence, start=1)}

    _guard_deadline(ctx, "generate")
    try:
        generated = await ctx.call_json(
            step="generate",
            system=prompts.ANSWER_SYSTEM_TEMPLATE.format(
                guidelines=prompts.guidelines_block(
                    await project_service.get_guideline_content(db, project.id)
                ),
                # 승인 교훈만 주입되고 주입한 것들의 `last_used_at` 이 갱신된다 (룰 7, `06 §3`).
                # `candidate` 는 여기 절대 들어오지 않는다 — 담당자 승인이 유일한 승격 경로다.
                lessons=prompts.lessons_block(
                    await lesson_service.load_for_prompt(db, project=project)
                ),
            ),
            user=prompts.answer_user_prompt(
                evidence=evidence, aliases=aliases, question_en=question.content_en
            ),
            schema=SentencesOut,
            model=env_settings.llm_model_answer,
        )
    except LLMSchemaError:
        logger.warning("④ 구조화 출력 재시도 소진: question=%s", question.id)
        return await _forced_red(
            ctx,
            HELD_REASON_SCHEMA_FAILED,
            evidence=evidence,
            sim_raw=sim_raw,
            search_score=search_score,
            similar_official_qa_id=similar_official_qa_id,
        )

    if generated["not_answerable"] or not generated["sentences"]:
        return await _forced_red(
            ctx,
            HELD_REASON_NO_EVIDENCE,
            evidence=evidence,
            sim_raw=sim_raw,
            search_score=search_score,
            similar_official_qa_id=similar_official_qa_id,
        )

    if generated["conflict"]:
        return await _forced_red(
            ctx,
            HELD_REASON_CONFLICT,
            evidence=evidence,
            sim_raw=sim_raw,
            search_score=search_score,
            similar_official_qa_id=similar_official_qa_id,
        )

    # 인용 id 실재 검증 — 환각 방어 1겹의 마지막 관문 (`06 §7`).
    # `[EVIDENCE]` 에 없는 id 는 제거하고, 인용이 비게 된 문장은 **supported=false 로 확정**한다.
    sentences = [
        _Sentence(
            index=index,
            text_en=item["text_en"],
            text_ko=item["text_ko"],
            aliases=[alias for alias in item["chunk_ids"] if alias in aliases],
        )
        for index, item in enumerate(generated["sentences"], start=1)
    ]
    original_count = len(sentences)

    # --- ⑤ 근거 검증 (G 산출) — **생성과 분리된 모델**로 부른다 --------------------------
    verifiable = [sentence for sentence in sentences if sentence.aliases]
    deadline_skipped_verify = False

    if verifiable and ctx.over_deadline():
        # 데드라인 초과 — 그 시점까지의 결과로 🟡 발행 (안전망, `06 §6`).
        deadline_skipped_verify = True
        logger.warning("데드라인 초과로 ⑤ 를 건너뛴다: question=%s", question.id)
    elif verifiable:
        # ⚠️ 인용 청크는 **자르지 않고 전문**을 넘긴다. `QUOTE_MAX_LENGTH` 는 화면
        # 하이라이트용 스니펫 길이(`05 §6` `citations[].quote`)이지 검증 근거가 아니다.
        # 그것으로 자르면 ④ 는 청크 전문을 보고 쓴 문장을 ⑤ 는 앞부분만 보고 판정하게 돼,
        # 근거가 청크 뒤쪽에 있는 문장이 통째로 "근거 없음"이 된다 → G=0 → 멀쩡한 답변이
        # `low_confidence` 🔴 로 떨어진다 (배포본 실측: 🔴 10건 전부 이 경로였다).
        blocks = [
            (
                position,
                sentence.text_en,
                [aliases[alias].content for alias in sentence.aliases],
            )
            for position, sentence in enumerate(verifiable, start=1)
        ]
        verdicts = await ctx.call_json(
            step="verify",
            system=prompts.VERIFY_SYSTEM,
            user=prompts.verify_user_prompt(blocks),
            schema=VerdictsOut,
            model=env_settings.llm_model_verify,
        )
        by_position = {item["index"]: bool(item["supported"]) for item in verdicts["verdicts"]}
        for position, sentence in enumerate(verifiable, start=1):
            sentence.supported = by_position.get(position, False)

    if deadline_skipped_verify:
        return await _publish_generated(
            ctx,
            grade=GRADE_YELLOW,
            held_reason=None,
            published=sentences,
            original_count=original_count,
            removed=[],
            evidence=evidence,
            sim_raw=sim_raw,
            search_score=search_score,
            # G 를 산출하지 못했다. 100 으로 채우면 검증하지 않은 근거를 주장하는 셈이므로
            # NULL 로 두고 매칭률도 내지 않는다 (`05 §6` 의 "매칭률 없음"과 같은 표기).
            g_raw=None,
            g_final=None,
            similar_official_qa_id=similar_official_qa_id,
            matching_rate=None,
            deadline_exceeded=True,
        )

    supported_count = sum(1 for sentence in sentences if sentence.supported)

    # ⛔ 분모는 **생성 시점 원본 문장 수**로 고정이다. 프루닝해도 줄지 않는다 (`06 §2` ⑤).
    g_raw = grading.grounding_score(supported_count, original_count)
    grounding_min = int(ctx.setting("grounding_min"))

    # --- 프루닝 (발행 품질을 위한 조치일 뿐 매칭률과 무관하다) ----------------------------
    removed = [sentence.text_en for sentence in sentences if not sentence.supported]
    all_supported_required = grading.requires_all_supported(original_count)

    if all_supported_required or grading.should_prune(
        g_raw, original_count, grounding_min=grounding_min
    ):
        published = [sentence for sentence in sentences if sentence.supported]
    else:
        published = list(sentences)
        removed = []

    # G_final 은 프루닝 **후에도** G_raw 와 같다. 다르게 나오면 분모가 흔들린 것이다.
    g_final = g_raw
    rate = grading.matching_rate(search_score, g_final)

    # --- ⑥ 등급 ----------------------------------------------------------------------
    if all_supported_required and supported_count < original_count:
        # 1~2문장 구간은 비율 통계가 의미를 갖지 못한다 — 하나라도 무근거면 🔴 (룰 1).
        grade = GRADE_RED
    else:
        grade = grading.grade_for(
            rate,
            green_threshold=int(ctx.setting("green_threshold")),
            yellow_threshold=int(ctx.setting("yellow_threshold")),
        )

    if not published:
        # 지우고 남는 내용이 없으면 🔴 이다 (룰 1).
        grade = GRADE_RED

    held_reason = HELD_REASON_LOW_CONFIDENCE if grade == GRADE_RED else None
    degraded = False

    # --- DND 강등 — **`low_confidence` 만** (룰 6, D2) ---------------------------------
    if (
        grade == GRADE_RED
        and published
        and dnd.should_degrade(
            datetime.now(UTC),
            away_mode=project.away_mode,
            timezone_name=answerer.timezone,
            dnd_start=str(ctx.setting("dnd_start")),
            dnd_end=str(ctx.setting("dnd_end")),
        )
    ):
        grade = GRADE_YELLOW
        degraded = True
        # 🟡 로 발행되는 답변에 held_reason 을 남기면 `05 §6` 의 "보류였다가 담당자가 답한
        # 질문"(held_info != null && status == answered)과 화면에서 구분되지 않는다.
        # 강등 사실은 `degraded_from_red` 와 `question.graded` 이벤트가 증적으로 남긴다.
        held_reason = None

    return await _publish_generated(
        ctx,
        grade=grade,
        held_reason=held_reason,
        published=published,
        original_count=original_count,
        removed=removed,
        evidence=evidence,
        sim_raw=sim_raw,
        search_score=search_score,
        g_raw=g_raw,
        g_final=g_final,
        similar_official_qa_id=similar_official_qa_id,
        matching_rate=rate,
        degraded=degraded,
    )


def _guard_deadline(ctx: _Ctx, step: str) -> None:
    """발행할 결과가 아직 없는 구간의 데드라인 검사 → 총 실패 (D23)."""
    if ctx.over_deadline():
        raise PipelineDeadlineExceeded(
            f"{step} 이전에 데드라인을 넘겼고 발행할 결과가 없다 (elapsed_ms={ctx.elapsed_ms()})"
        )


async def _record_reuse_missed(ctx: _Ctx, best_similarity: float) -> None:
    """재질문 즉답률(D26)의 **분모**. 후보를 찾았는데 재사용하지 않은 모든 경우에 남긴다.

    ⚠️ 후보의 기준은 `similar_threshold` 이상이다 — 무관한 질문까지 분모에 넣으면
    지표가 "공식 Q&A 가 하나라도 있으면 계속 떨어지는" 값이 되어 의미를 잃는다 (`04 §5`).
    """
    await event_service.record_event(
        ctx.db,
        project_id=ctx.project.id,
        type=event_service.EVENT_ANSWER_REUSE_MISSED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=ctx.question.id,
        payload={"best_similarity": best_similarity},
    )


async def _forced_red(
    ctx: _Ctx,
    held_reason: str,
    *,
    evidence: list[EvidenceChunk],
    sim_raw: float | None,
    search_score: int | None,
    similar_official_qa_id: UUID | None,
) -> _Outcome:
    """강제 🔴 4종 (`02` 룰 1). **DND 에서도 강등되지 않는다** (D2)."""
    return await _publish_generated(
        ctx,
        grade=GRADE_RED,
        held_reason=held_reason,
        published=[],
        original_count=0,
        removed=[],
        evidence=evidence,
        sim_raw=sim_raw,
        search_score=search_score,
        g_raw=None,
        g_final=None,
        similar_official_qa_id=similar_official_qa_id,
    )


async def _publish_reused(ctx: _Ctx, official_qa: OfficialQA, similarity: float) -> _Outcome:
    """재사용 즉답 (룰 4, D11) — **검색·생성 전부 스킵**.

    - `answer_ko` 는 확정 당시 한국어 원문 **그대로**다. 재번역 금지 (D5).
    - `state='verified'` · `expires_at=NULL` · **카드 미생성** · 만료 스위퍼 대상 아님.
    - 등급은 🟢 로 표기하되 `matching_rate` 는 NULL 이다("공식 확정 답변").
    """
    db = ctx.db
    question = ctx.question

    answer = Answer(
        question_id=question.id,
        grade=GRADE_GREEN,
        matching_rate=None,
        search_score=None,
        grounding_score=None,
        # 원시 코사인이라는 점은 같으므로 캘리브레이션 근거로 남긴다(리스케일하지 않는다).
        sim_raw=similarity,
        held_reason=None,
        question_struct=None,
        state=ANSWER_STATE_VERIFIED,
        content_ko=official_qa.answer_ko,
        content_en=official_qa.answer_en,
        source=ANSWER_SOURCE_REUSED,
        official_qa_id=official_qa.id,
        similar_official_qa_id=None,
        degraded_from_red=False,
        expires_at=None,
    )
    db.add(answer)

    official_qa.reuse_count += 1

    previous = question.status
    question.status = QUESTION_STATUS_ANSWERED
    await db.flush()

    # ⚠️ 파이프라인 이벤트는 전부 **질문에 붙인다** — `05 §13` 의 타임라인 조회가
    #    `?entity_type=question&entity_id=q-9` 이기 때문이다. 답변에 붙이면 그 질문의
    #    타임라인에서 사라진다. 질문당 답변은 1행이므로(`04 §7`) 잃는 정보가 없고,
    #    답변 식별자는 payload 의 `answer_id` 로 남긴다.
    await event_service.record_event(
        db,
        project_id=ctx.project.id,
        type=event_service.EVENT_ANSWER_REUSED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={
            "answer_id": str(answer.id),
            "official_qa_id": str(official_qa.id),
            "similarity": similarity,
        },
    )
    # ⚠️ **등급이 먼저, 상태 전이가 나중**이다 (`07 §완료 기준` 타임라인 순서).
    #    등급이 상태를 정하므로(🔴 이면 held, 아니면 answered) 이 순서라야 `05 §13` 타임라인이
    #    인과 그대로 읽힌다. 같은 트랜잭션이라 `clock_timestamp()` 가 이 순서를 보존한다.
    await _record_graded(
        ctx,
        answer=answer,
        grade=GRADE_GREEN,
        matching_rate=None,
        sim_raw=similarity,
        search_score=None,
        g_raw=None,
        g_final=None,
        removed=[],
        source=ANSWER_SOURCE_REUSED,
        held_reason=None,
        degraded=False,
        deadline_exceeded=False,
    )
    await _record_status_changed(ctx, previous, QUESTION_STATUS_ANSWERED)

    # 재사용은 **카드를 만들지 않으므로**(D11) 담당자 알림도 없다. 질문자에게만 알린다.
    await notification_service.notify_answer_completed(
        db, question=question, answer=answer, held=False
    )
    sse_manager.queue_answer_completed(
        db,
        asker_id=question.asker_id,
        question_id=question.id,
        grade=GRADE_GREEN,
        status=QUESTION_STATUS_ANSWERED,
    )
    return _Outcome(
        project_id=ctx.project.id,
        question_id=question.id,
        grade=GRADE_GREEN,
        status=QUESTION_STATUS_ANSWERED,
    )


async def _publish_generated(
    ctx: _Ctx,
    *,
    grade: str,
    held_reason: str | None,
    published: list[_Sentence],
    original_count: int,
    removed: list[str],
    evidence: list[EvidenceChunk],
    sim_raw: float | None,
    search_score: int | None,
    g_raw: int | None,
    g_final: int | None,
    similar_official_qa_id: UUID | None,
    matching_rate: int | None = None,
    degraded: bool = False,
    deadline_exceeded: bool = False,
    skip_struct: bool = False,
) -> _Outcome:
    """⑧ 발행 (`06 §2` ⑧).

    🔴 은 질문자에게 발행하지 않지만 `answers` 행은 **초안으로 남긴다** — 카드가 그것을
    담당자에게 보여준다 (`04 §2`). ko 본문은 ④ 의 `text_ko` 를 결합해 만든다:
    **여기서 번역 LLM 을 부르지 않는다** (4회 → 3회 최적화, `06 §0`).
    """
    db = ctx.db
    question = ctx.question
    is_red = grade == GRADE_RED

    # --- ⑦ 🔴 질문 구조화 ------------------------------------------------------------
    question_struct: dict[str, Any] | None = None
    if is_red and not skip_struct and not ctx.over_deadline(red_path=True):
        question_struct = await ctx.call_json(
            step="structure",
            system=prompts.STRUCT_SYSTEM,
            user=prompts.struct_user_prompt(
                question_en=question.content_en or question.content_ko,
                held_reason=held_reason or HELD_REASON_LOW_CONFIDENCE,
                evidence=evidence,
            ),
            schema=QuestionStructOut,
            model=env_settings.llm_model_struct,
        )
    elif is_red:
        logger.warning(
            "⑦ 구조화를 건너뛴다: question=%s skip_struct=%s elapsed_ms=%s",
            question.id,
            skip_struct,
            ctx.elapsed_ms(),
        )

    expire_hours = int(ctx.setting("draft_expire_hours"))
    answer = Answer(
        question_id=question.id,
        grade=grade,
        matching_rate=matching_rate,
        search_score=search_score,
        grounding_score=g_final,
        sim_raw=sim_raw,
        held_reason=held_reason,
        question_struct=question_struct,
        state=ANSWER_STATE_DRAFT,
        content_ko=" ".join(sentence.text_ko for sentence in published),
        content_en=" ".join(sentence.text_en for sentence in published),
        source=ANSWER_SOURCE_GENERATED,
        official_qa_id=None,
        similar_official_qa_id=similar_official_qa_id,
        degraded_from_red=degraded,
        expires_at=datetime.now(UTC) + timedelta(hours=expire_hours),
    )
    db.add(answer)
    await db.flush()

    _add_citations(db, answer=answer, published=published, evidence=evidence)

    previous = question.status
    question.status = QUESTION_STATUS_HELD if is_red else QUESTION_STATUS_ANSWERED
    await db.flush()

    # ⚠️ **등급 → 발행 → 상태 전이** 순서다 (`07 §완료 기준` 타임라인). 등급이 원인이고
    #    나머지가 결과다 — 🔴 이면 발행하지 않고 상태도 `held` 가 되므로, 등급을 나중에
    #    적으면 `05 §13` 타임라인이 "발행했는데 그 다음에 등급을 매겼다"로 읽힌다.
    #    같은 트랜잭션이라 `clock_timestamp()` 가 이 순서를 보존한다.
    await _record_graded(
        ctx,
        answer=answer,
        grade=grade,
        matching_rate=matching_rate,
        sim_raw=sim_raw,
        search_score=search_score,
        g_raw=g_raw,
        g_final=g_final,
        removed=removed,
        source=ANSWER_SOURCE_GENERATED,
        held_reason=held_reason,
        degraded=degraded,
        deadline_exceeded=deadline_exceeded,
    )

    if not is_red:
        # 질문 스코프로 남긴다 — 이유는 `_publish_reused` 의 주석 참조.
        await event_service.record_event(
            db,
            project_id=ctx.project.id,
            type=event_service.EVENT_ANSWER_PUBLISHED,
            entity_type=event_service.ENTITY_QUESTION,
            entity_id=question.id,
            payload={
                "answer_id": str(answer.id),
                "grade": grade,
                "state": ANSWER_STATE_DRAFT,
            },
        )

    await _record_status_changed(ctx, previous, question.status)

    # 확인 카드 — 🟢 도 만든다. 큐가 최종 안전망이기 때문이다 (룰 6). 다만 🟢 은 브리핑·알림
    # 대상에서 제외되고, "맞았다" 2건으로 `recommend_approve` 가 될 때 비로소 브리핑에
    # 등장한다 (룰 1·3). 데드라인 초과로 발행된 🟡 도 카드를 만든다 (`06 §6` 안전망).
    card = await review_card_service.create_card(
        db,
        question=question,
        answer=answer,
        reason=review_card_service.CARD_REASON_BY_GRADE[grade],
    )

    # 질문자 `answer.completed` — 🔴 은 발행이 아니라 **보류 안내**다 (`04 §4`).
    await notification_service.notify_answer_completed(
        db, question=question, answer=answer, held=is_red
    )
    # 담당자 `card.created` — ⚠️ `reason='green'` 카드는 알림 대상이 아니다 (룰 1).
    # 판정은 `notifies_answerer` 한 곳에만 있다: 여기서 `reason == 'green'` 을 다시 쓰면
    # 브리핑(M6)이 같은 규칙을 또 구현하게 되고 두 곳이 어긋난다.
    if review_card_service.notifies_answerer(card):
        await notification_service.notify_card_created(
            db, card=card, question=question, project=ctx.project
        )

    sse_manager.queue_answer_completed(
        db,
        asker_id=question.asker_id,
        question_id=question.id,
        grade=grade,
        status=question.status,
    )
    return _Outcome(
        project_id=ctx.project.id,
        question_id=question.id,
        grade=grade,
        status=question.status,
    )


def _add_citations(
    db: AsyncSession,
    *,
    answer: Answer,
    published: list[_Sentence],
    evidence: list[EvidenceChunk],
) -> None:
    """별칭 → UUID 복원 (`06 §2` ④). 발행된 문장이 실제로 인용한 청크만 남긴다.

    ⚠️ `official_qa_id` 는 채우지 않는다 — D24 이후 항상 NULL 이며 `05 §6` `citations[]` 에
    대응 필드가 없다.
    """
    aliases = {f"ch-{index}": chunk for index, chunk in enumerate(evidence, start=1)}
    seen: set[UUID] = set()

    for sentence in published:
        for alias in sentence.aliases:
            chunk = aliases.get(alias)
            if chunk is None or chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            db.add(
                AnswerCitation(
                    answer_id=answer.id,
                    chunk_id=chunk.chunk_id,
                    official_qa_id=None,
                    quote=chunk.content[: prompts.QUOTE_MAX_LENGTH],
                    similarity=chunk.sim_raw,
                )
            )


async def _record_status_changed(ctx: _Ctx, previous: str, current: str) -> None:
    """`04 §6.1` 의 **모든 전이**에서 발행한다."""
    await event_service.record_event(
        ctx.db,
        project_id=ctx.project.id,
        type=event_service.EVENT_QUESTION_STATUS_CHANGED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=ctx.question.id,
        payload={"from": previous, "to": current},
    )


async def _record_graded(
    ctx: _Ctx,
    *,
    answer: Answer,
    grade: str,
    matching_rate: int | None,
    sim_raw: float | None,
    search_score: int | None,
    g_raw: int | None,
    g_final: int | None,
    removed: list[str],
    source: str,
    held_reason: str | None,
    degraded: bool,
    deadline_exceeded: bool,
) -> None:
    """등급 산출 증적 (`04 §5`, `06 §2` ⑤).

    `S_raw`·`S`·`G_raw`·`G_final`·`removed_sentences` 를 **전부** 남긴다 —
    환각 방어 2겹의 증적이자 캘리브레이션 재산출의 근거다. 하나라도 빠지면 사후에
    "어느 겹에서 걸렀는지"를 되짚을 수 없다.
    """
    await event_service.record_event(
        ctx.db,
        project_id=ctx.project.id,
        type=event_service.EVENT_QUESTION_GRADED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=ctx.question.id,
        payload={
            "answer_id": str(answer.id),
            "grade": grade,
            "matching_rate": matching_rate,
            "S_raw": sim_raw,
            "S": search_score,
            "G_raw": g_raw,
            "G_final": g_final,
            "removed_sentences": removed,
            "source": source,
            "held_reason": held_reason,
            "degraded_from_red": degraded,
            "deadline_exceeded": deadline_exceeded,
            "elapsed_ms": ctx.elapsed_ms(),
            "steps": {name: elapsed for name, elapsed in ctx.steps},
        },
    )


async def load_answer(db: AsyncSession, question_id: UUID) -> Answer | None:
    """질문의 답변 1행 (`04 §7` UNIQUE(question_id))."""
    return await db.scalar(select(Answer).where(Answer.question_id == question_id))
