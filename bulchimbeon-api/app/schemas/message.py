"""대화 메시지 스키마 (`05 §6.1` 과 1:1).

`sender.role` 은 **이 프로젝트에서의** `project_members.role` 이다 — 담당자 교체(D16)로
바뀔 수 있는 현재 역할이며, 프론트는 이 값으로 말풍선 좌우·배지를 가른다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# 질문 본문과 같은 상한 (`schemas/question.py` MAX_QUESTION_LENGTH).
MAX_MESSAGE_LENGTH = 4000


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """공백만인 본문은 거부한다 — 전역 핸들러가 400 `VALIDATION_ERROR` 로 내린다 (`05 §1.4`)."""
        if not value.strip():
            raise ValueError("content 는 공백만으로 이루어질 수 없습니다.")
        return value


class MessageSender(BaseModel):
    id: UUID
    name: str
    role: Literal["answerer", "asker"]


class MessageOut(BaseModel):
    id: UUID
    content: str
    sender: MessageSender
    created_at: datetime


class MessageListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투. `items` 는 **created_at 오름차순** — 채팅 화면 순서다."""

    items: list[MessageOut]
    total: int
    limit: int
    offset: int
