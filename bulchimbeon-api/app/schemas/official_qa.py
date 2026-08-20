"""공식 Q&A 스키마 (`05 §9`).

⚠️ `answer_ko` 는 **확정 당시의 한국어 원문**이다 (룰 4·D5). 재사용 시 영어 저장본을
다시 번역하지 않으므로 이 필드가 곧 재질문 즉답의 본문이 된다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

OfficialQAStatus = Literal["active", "under_review", "archived"]


class OfficialQACreate(BaseModel):
    """`POST /projects/{id}/official-qas` — 담당자 직접 등록 (`05 §9`).

    공백만인 본문은 400 `VALIDATION_ERROR` 다 — pydantic ValueError 를 전역 핸들러가
    계약 포맷으로 바꾼다 (`05 §1.4`, `main.py`).
    """

    question_ko: str = Field(min_length=1)
    answer_ko: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_blank(self) -> "OfficialQACreate":
        if not self.question_ko.strip() or not self.answer_ko.strip():
            raise ValueError("question_ko/answer_ko 는 공백만일 수 없습니다.")
        return self


class OfficialQAListItem(BaseModel):
    id: UUID
    question_ko: str
    question_en: str
    answer_ko: str
    answer_en: str
    status: OfficialQAStatus
    correct_count: int
    reuse_count: int
    created_at: datetime


class OfficialQAListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투. `archived` 는 기본으로 나타나지 않는다 (`05 §9`)."""

    items: list[OfficialQAListItem]
    total: int
    limit: int
    offset: int


class OfficialQADetail(OfficialQAListItem):
    """상세 — ko/en 쌍 + **출처 답변** + reuse_count + status (`05 §9`).

    직접 등록(`POST`)은 원천 답변이 없으므로 출처 두 필드가 모두 `null` 이다.
    """

    source_answer_id: UUID | None
    source_question_id: UUID | None


class OfficialQAArchived(BaseModel):
    """`DELETE /official-qas/{id}` 200 — 물리 삭제가 아니라 상태 전이다."""

    id: UUID
    status: OfficialQAStatus
    archived_at: datetime
