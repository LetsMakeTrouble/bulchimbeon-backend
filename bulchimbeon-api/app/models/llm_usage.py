"""llm_usage (LLM 호출 사용량·비용) — 운영 계측용.

**LLM 호출 1회 = 1행**이다. 종전에는 이 정보가 어디에도 남지 않았다 — 호출 수는
`pipeline/quota.py` 의 **프로세스 메모리 딕셔너리**라 재시작하면 0 이 됐고, 토큰과 비용은
아예 기록되지 않았다. 그 상태에서는 "어떤 계정이 얼마를 썼나"에 답할 수 없다.

> ### 왜 `events` 가 아니라 별도 테이블인가
> 룰 4("모든 상태 변화는 `events` 에")의 대상은 **상태 변화**다. LLM 호출은 상태 변화가
> 아니라 **자원 소비**이고, 조회 패턴도 다르다 — events 는 타임라인(프로젝트+시각)이고
> 여기는 집계(주체+기간별 SUM)다. 같은 테이블에 섞으면 둘 다 느려진다.

> ### `cost_usd` 는 기록 시점 단가로 굳힌다
> 조회할 때 다시 계산하지 않는다. 단가가 바뀌면 지난달 비용까지 소급 변경되기 때문이다
> (`services/llm/pricing.py`).

⚠️ **`user_id` 는 nullable 이다.** 스케줄러가 부르는 교훈 추출처럼 사람이 촉발하지 않은
호출이 있다. 그 행도 프로젝트 비용에는 잡혀야 하므로 `project_id` 만 필수다.
"""

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin

# `step` 에 들어가는 값. 파이프라인 단계 + 파이프라인 밖 경로.
STEP_TRANSLATE = "translate"
STEP_REUSE_GATE = "reuse_gate"
STEP_GENERATE = "generate"
STEP_VERIFY = "verify"
STEP_STRUCTURE = "structure"
STEP_ANSWER_TRANSLATE = "answer_translate"
STEP_LESSON = "lesson"
STEP_EMBED = "embed"


class LLMUsage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "llm_usage"
    __table_args__ = (
        # 일일 한도 판정과 프로젝트 비용 집계가 함께 타는 경로.
        Index("ix_llm_usage_project_created", "project_id", "created_at"),
        # "이 계정이 얼마나 썼나" 조회. user_id 가 NULL 인 행은 여기 안 걸린다.
        Index("ix_llm_usage_user_created", "user_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    # 사람이 촉발하지 않은 호출(스케줄러 경유 교훈 추출 등)은 NULL 이다.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # 질문 파이프라인 밖의 호출(확정문 번역·인제스트 임베딩)은 NULL 이다.
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("questions.id"), nullable=True
    )

    step: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ⚠️ `output_tokens` 에 **포함된** 값이다. 합계를 낼 때 따로 더하면 이중 계상이다.
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 1회 호출 비용. 소수 6자리면 최소 단가(luna 입력 $0.2/1M)에서도 유효숫자가 남는다.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
