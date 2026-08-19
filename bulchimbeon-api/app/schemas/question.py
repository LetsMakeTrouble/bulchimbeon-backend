"""질문·답변 스키마 (`05 §6` 과 1:1).

⚠️ **계약서에 없는 필드를 만들지 않는다.** `05` 는 프론트 팀과의 계약이다 (룰 7 주변).
없는 값은 `null` 로 내려보내되 필드 자체는 유지한다 — 프론트가 키 존재를 전제로 렌더한다.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

MAX_QUESTION_LENGTH = 4000
MAX_FEEDBACK_NOTE_LENGTH = 1000

Urgency = Literal["normal", "urgent"]
Grade = Literal["green", "yellow", "red"]
QuestionStatus = Literal["processing", "answered", "held", "failed"]
AnswerState = Literal["draft", "verified", "under_review", "expired", "rejected"]
HeldReason = Literal["conflict", "no_evidence", "low_confidence", "schema_failed", "quota_exceeded"]


class QuestionCreate(BaseModel):
    content_ko: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    urgency: Urgency = "normal"


class QuestionAccepted(BaseModel):
    """202 — 파이프라인 비동기 시작 (`05 §6`, D3 "질문 POST 는 202 즉시 반환").

    ⚠️ `suggest_urgent` 는 **접수 시점의 값**이라 항상 `false` 다. 이 플래그를 만드는 것은
    파이프라인 ① 이고(`06 §2` ①), D3 가 즉시 반환을 요구하므로 202 는 ① 을 기다리지 않는다.
    파이프라인이 채운 값은 `questions.suggest_urgent` 에 남는다.
    """

    question_id: UUID
    status: QuestionStatus
    suggest_urgent: bool
    created_at: datetime


class UrgencyPatch(BaseModel):
    urgency: Urgency


class UrgencyPatched(BaseModel):
    id: UUID
    urgency: Urgency
    status: QuestionStatus


FeedbackVerdict = Literal["correct", "different"]


class FeedbackSummary(BaseModel):
    """`05 §6`. `feedbacks` 테이블이 원천이며 `my_feedback` 은 **조회자 기준**이다."""

    correct: int = 0
    different: int = 0
    my_feedback: FeedbackVerdict | None = None


class FeedbackCreate(BaseModel):
    """크로스체크 (`05 §6`) — **"달랐다"는 사유 한 줄이 필수**다."""

    verdict: FeedbackVerdict
    note: str | None = Field(default=None, max_length=MAX_FEEDBACK_NOTE_LENGTH)

    @model_validator(mode="after")
    def _note_required_for_different(self) -> "FeedbackCreate":
        if self.verdict == "different" and not (self.note or "").strip():
            raise ValueError("different 피드백에는 사유(note)가 필요합니다.")
        return self


class FeedbackResponse(BaseModel):
    """`05 §6` 200.

    **갱신된 `feedback_summary` 를 항상 포함한다** — 프론트 낙관적 업데이트의 전제이며
    `GET /questions/{id}` 재조회를 불필요하게 만든다.
    """

    answer_id: UUID
    answer_state: AnswerState
    message: str
    feedback_summary: FeedbackSummary
    recommend_approve: bool


class Citation(BaseModel):
    """`05 §6` `citations[]` — 기능 2.2 근거 원문 열람의 전제.

    `document_id`·`document_version_id` 없이는 열람 URL 을 만들 수 없으므로 **필수 제공**이다.
    `similarity` 는 **원시 코사인**이다 — `matching_rate` 와 스케일이 다르므로 % 로 표시하지 않는다.
    """

    id: UUID
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    doc_title: str
    version_no: int
    heading_path: list[str]
    page_no: int | None
    quote: str
    similarity: float


class GradeAccuracy(BaseModel):
    """등급별 실측 정확도 (D25) — `(verified 또는 correct 피드백 ≥1건) / 해당 등급 전체 발행 수`.

    `05 §6` `answer.accuracy_context` 와 `05 §13` `metrics.grade_accuracy[]` 가 **동일한 아이템
    shape** 이라고 계약서가 못박았으므로 정의를 한 곳에만 둔다. 두 파일에 각자 정의하면 M7 에서
    필드 하나가 어긋나고 그 차이는 프론트가 화면에서 발견한다. (이 모듈이 스키마 계층의
    바닥이라 여기 둔다 — `schemas/review_card.py` 도 `Grade`·`Citation` 을 여기서 가져간다.)

    ⚠️ **표본 30건 미만이면 `verified_rate` 는 반드시 `null`** 이고 `sufficient=false` 이며
    `message` 가 채워진다. 데모 규모에서 나온 비율을 정확도로 읽히게 두지 않는 것이 이 지표의
    존재 이유다 — 프론트는 `sufficient:false` 면 `%` 대신 `message` 를 표시한다.
    """

    grade: Grade
    verified_rate: float | None
    sample: int
    window_days: int
    sufficient: bool
    message: str | None

    @model_validator(mode="after")
    def _rate_absent_when_insufficient(self) -> "GradeAccuracy":
        """숫자와 "표본 부족"이 동시에 내려가면 프론트가 어느 쪽을 믿어야 할지 알 수 없다."""
        if not self.sufficient and self.verified_rate is not None:
            raise ValueError("sufficient=false 인 아이템의 verified_rate 는 null 이어야 한다.")
        return self


class AnswerOut(BaseModel):
    id: UUID
    grade: Grade
    state: AnswerState
    matching_rate: int | None
    search_score: int | None
    grounding_score: int | None
    content_ko: str
    content_en: str
    source: Literal["generated", "reused"]
    degraded_from_red: bool
    expires_at: datetime | None
    disclaimer: str | None
    citations: list[Citation]
    feedback_summary: FeedbackSummary
    official_qa: "OfficialQARef | None" = None
    # `05 §6` 정확도 실적 표기(선택 노출). §13 `metrics.grade_accuracy[]` 와 동일 shape 이다.
    accuracy_context: GradeAccuracy | None = None


class OfficialQARef(BaseModel):
    """재사용 답변의 원본 (`05 §6` 재사용 표)."""

    id: UUID
    question_ko: str
    reuse_count: int


class SimilarOfficialQA(BaseModel):
    """`similar_threshold` ~ `reuse_threshold` 구간에서 첨부된다 (룰 4, D24)."""

    id: UUID
    question_ko: str
    answer_ko: str
    similarity: float


class HeldInfo(BaseModel):
    reason: HeldReason
    message: str
    card_status: Literal["pending", "deferred", "resolved"]


class FailureInfo(BaseModel):
    """`held_info` 와 대칭 구조 (D23). `reason` 은 **로깅용 코드**다."""

    reason: str
    message: str
    card_status: Literal["pending", "deferred", "resolved"]


class AskedBy(BaseModel):
    id: UUID
    name: str


class QuestionDetail(BaseModel):
    """`05 §6` GET /questions/{id} — 핵심 화면."""

    id: UUID
    content_ko: str
    content_en: str | None
    urgency: Urgency
    status: QuestionStatus
    asked_by: AskedBy
    answer: AnswerOut | None
    similar_official_qa: SimilarOfficialQA | None
    held_info: HeldInfo | None
    failure_info: FailureInfo | None


class QuestionListItem(BaseModel):
    """`05 §6` 목록 아이템 — **이것만으로 질문자 채팅 목록을 그릴 수 있다.**

    `asked_by` 는 담당자의 전체 목록에서 행 단위로 질문자를 구분하기 위한 것이다.
    상세의 `asked_by` 와 동일 shape 이며, 하위호환 **추가** 필드다.
    """

    id: UUID
    content_ko: str
    asked_by: AskedBy
    status: QuestionStatus
    grade: Grade | None
    matching_rate: int | None
    state: AnswerState | None
    created_at: datetime
    feedback_summary: FeedbackSummary | None


class QuestionListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투."""

    items: list[QuestionListItem]
    total: int
    limit: int
    offset: int


AnswerOut.model_rebuild()


class QuestionStruct(BaseModel):
    """⑦ 결과 (`06 §2` ⑦). M4 가 `review_cards.question_struct` 로 복사한다."""

    background: str
    question: str
    options: list[str]

    @classmethod
    def from_jsonb(cls, value: dict[str, Any] | None) -> "QuestionStruct | None":
        return cls.model_validate(value) if value else None
