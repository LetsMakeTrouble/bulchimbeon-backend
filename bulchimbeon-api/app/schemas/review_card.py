"""확인 카드 큐 스키마 (`05 §7` 과 1:1).

> ### 큐 화면은 **목록만으로 완성된다**
> `ReviewCardListItem` 이 곧 카드 행의 렌더 계약이다. 상세를 부를 이유가 없어야 한다 —
> `GET /review-cards/{id}` 는 최초 조회 시 `first_viewed_at` 을 기록하므로 프리페치하면
> 카드 처리 시간 지표(`05 §13` `card_handle_30s_rate`)가 파괴된다.

⚠️ **계약서에 없는 필드를 만들지 않는다.** 없는 값은 `null` 로 내려보내되 키는 유지한다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.question import AskedBy, Citation, Grade, QuestionStruct

CardReason = Literal["green", "yellow", "red", "feedback", "doc_update", "failed"]
CardStatus = Literal["pending", "deferred", "resolved"]
CardResolution = Literal["approved", "edited", "rejected", "kept"]

# `05 §7` — 목록 아이템의 질문 미리보기 길이.
PREVIEW_LENGTH = 120

MAX_ANSWER_LENGTH = 8000
MAX_REASON_LENGTH = 2000


def preview(text: str | None) -> str | None:
    """질문 앞부분 미리보기. `content_en` 은 파이프라인이 죽으면 없을 수 있다 (D23)."""
    return None if text is None else text[:PREVIEW_LENGTH]


class ReviewCardListItem(BaseModel):
    """`05 §7` 큐 목록 아이템 — 정렬은 **승인 추천 → 긴급 → 오래된 순**."""

    id: UUID
    reason: CardReason
    status: CardStatus
    is_urgent: bool
    recommend_approve: bool

    # 원본 답변 등급. `reason='failed'` 면 답변 자체가 없으므로 `null` 이다.
    grade: Grade | None

    # ⚠️ `reason='failed'` 는 ① 번역 이전에 죽었을 수 있어 en 이 `null` 이다.
    #    ko 로 대체하지 않는다 — 담당자 화면이 한국어를 영어로 오인한다.
    question_preview_en: str | None
    question_preview_ko: str

    created_at: datetime
    first_viewed_at: datetime | None  # null → "NEW" 뱃지


class ReviewCardListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투."""

    items: list[ReviewCardListItem]
    total: int
    limit: int
    offset: int


class CardQuestion(BaseModel):
    id: UUID
    content_en: str | None
    content_ko: str


class DraftAnswer(BaseModel):
    """`citations[]` 는 `05 §6` 과 **동일 스키마**다 — 팝업에서 근거 원문을 연다."""

    content_en: str
    citations: list[Citation]


class PendingFeedback(BaseModel):
    """담당자가 카드를 여는 사이에 들어온 미해소 피드백 (룰 9).

    담당자가 저장하면 이 건들이 함께 `resolved` 처리된다 — 담당자 저장이 항상 우선이다.
    """

    id: UUID
    verdict: Literal["correct", "different"]
    note: str | None
    user: AskedBy
    created_at: datetime


class ReviewCardDetail(BaseModel):
    """`05 §7` 상세 — 목록 아이템의 **상위 집합**이다."""

    id: UUID
    reason: CardReason
    status: CardStatus
    is_urgent: bool
    recommend_approve: bool
    grade: Grade | None
    created_at: datetime
    first_viewed_at: datetime | None
    deferred_until: datetime | None
    answer_id: UUID | None
    question: CardQuestion

    # 🔴 전달용이라 `reason='red'` 에만 채워진다 (`05 §7`). `answer-option` 의 전제이기도 하다.
    question_struct: QuestionStruct | None
    draft_answer: DraftAnswer | None
    pending_feedbacks: list[PendingFeedback]


# --- 액션 요청 -------------------------------------------------------------------------
class CardEditRequest(BaseModel):
    """담당자 입력은 **영어**다. 서버가 ko 를 만들고 그 ko 가 확정 원문으로 고정된다 (D5)."""

    content_en: str = Field(min_length=1, max_length=MAX_ANSWER_LENGTH)


class CardAnswerOptionRequest(BaseModel):
    """`question_struct.options[index]` 로 확정 — "30초 컷"의 실현 수단 (`05 §7.2`)."""

    index: int = Field(ge=0)


class CardKeepRequest(BaseModel):
    reason_en: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class CardRejectRequest(BaseModel):
    reason_en: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class CardDeferRequest(BaseModel):
    """`until` 생략 시 기본값 = **다음 `briefing_hour`** (담당자 timezone 기준, D15)."""

    until: datetime | None = None


class BulkKeepRequest(BaseModel):
    document_version_id: UUID


# --- 액션 응답 -------------------------------------------------------------------------
class CardAnswerOut(BaseModel):
    """`05 §7.1` 처리 응답의 `answer`."""

    id: UUID
    state: Literal["draft", "verified", "under_review", "expired", "rejected"]
    content_ko: str
    content_en: str


class CardActionResponse(BaseModel):
    """approve · edit · keep · reject 공통 shape (`05 §7.1`).

    - `official_qa_id` · `lesson_candidate_id` 는 해당 없으면 `null` 이다.
      교훈 추출은 M6 범위이므로 지금은 항상 `null` 이다 (`07` M6).
    - `resolved_feedbacks` 는 이 처리로 함께 해소된 미해소 피드백 건수다 (룰 9).
    """

    answer: CardAnswerOut | None
    official_qa_id: UUID | None
    lesson_candidate_id: UUID | None
    resolved_feedbacks: int


class SelectedOption(BaseModel):
    index: int
    text: str


class CardAnswerOptionResponse(CardActionResponse):
    """`05 §7.2` — edit 응답 + 선택 내역 echo."""

    selected_option: SelectedOption


class CardDeferResponse(BaseModel):
    """`05 §7.3`."""

    id: UUID
    status: CardStatus
    deferred_until: datetime | None


class BulkKeepResponse(BaseModel):
    """문서 갱신 재검토 묶음 전체 유지 (룰 5).

    ⚠️ `05 §7` 은 요청 body 만 규정하고 응답 shape 을 남기지 않았다. 카드 하나짜리 응답
    (`CardActionResponse`)을 N 번 담으면 묶음의 의미가 흐려지므로 건수로 요약한다.
    """

    document_version_id: UUID
    kept_count: int
    resolved_feedbacks: int
