"""lessons (교훈 메모리) — `04 §2`.

담당자가 답변을 **수정**할 때 원답 vs 수정답의 차이에서 뽑아낸 한 줄 원칙이다 (`06 §3`).
`candidate` 로 태어나고 담당자 승인을 거쳐야 `approved` 가 되며, **`approved` 만 생성
프롬프트의 `[APPROVED LESSONS]` 에 주입된다** — `candidate` 는 어디에도 쓰이지 않는다.

⚠️ 삭제는 물리 삭제가 아니라 `status='deleted'` 전이다. 행이 남아야 `content_hash` 로
**동일 내용 재생성을 차단**할 수 있기 때문이다 (D8). 지운 교훈이 다음 수정에서 똑같이
다시 올라오면 담당자는 같은 판단을 영원히 반복하게 된다.

문서가 갱신되면 교훈은 지우지 않고 `needs_recheck=true` 로만 표시한다 (룰 5) — 근거가
바뀌었다는 사실과 원칙이 틀렸다는 사실은 다르고, 그 판단은 담당자 몫이다.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

LESSON_STATUS_CANDIDATE = "candidate"
LESSON_STATUS_APPROVED = "approved"
LESSON_STATUS_DELETED = "deleted"
LESSON_STATUSES = (
    LESSON_STATUS_CANDIDATE,
    LESSON_STATUS_APPROVED,
    LESSON_STATUS_DELETED,
)

_STATUS_CHECK = ", ".join(f"'{value}'" for value in LESSON_STATUSES)


class Lesson(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "lessons"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_CHECK})", name="ck_lessons_status"),
        # 목록 조회(`05 §10` `?status=`)와 주입용 approved 로드(`06 §3`)가 함께 타는 경로.
        Index("ix_lessons_project_id_status", "project_id", "status"),
        # 삭제 교훈 재생성 차단(D8)의 조회 경로 — 후보를 만들기 **전에** 매번 한 번씩 탄다.
        # 프로젝트 스코프 안에서만 비교한다: 같은 문장이라도 프로젝트가 다르면 다른 원칙이다.
        Index("ix_lessons_project_id_content_hash", "project_id", "content_hash"),
    )

    # ⚠️ 단독 인덱스를 달지 않는다 — 위 두 복합 인덱스가 모두 `project_id` 를 선두 컬럼으로
    # 쓰므로 접두사로 커버된다 (`review_cards.project_id` 와 같은 이유). `official_qas` 쪽에
    # `index=True` 가 붙어 있는 것은 거기엔 복합 인덱스가 없기 때문이다.
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )

    # 한 줄 원칙 (`04 §2`).
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 정규화 정의는 `app/utils/hashing.lesson_content_hash` 한 곳뿐이다 (`03 §5.2`).
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'candidate'"))

    # 문서 갱신 시 true (룰 5). 자동 삭제하지 않는다 — 재확인 요청 표시일 뿐이다.
    needs_recheck: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # 교훈은 답변 수정에서 파생되지만, 그 답변이 없어도 교훈은 남는다 — 담당자가 직접
    # 등록하는 경로가 열릴 수 있고 출처는 이력용 참조일 뿐이므로 nullable 이다 (`04 §2`).
    source_answer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("answers.id"), nullable=True
    )

    # 30개 초과 시 정리 제안(`05 §10` `cleanup_suggestions[]`)의 기준. 프롬프트에 실제로
    # 주입될 때 갱신되므로 **NULL = 승인된 뒤 한 번도 안 쓰인 교훈**이다.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
