"""briefing_runs (브리핑 중복 발송 방지) — `04 §2`·`04 §7`.

> ### 중복 발송은 **락이 아니라 제약**으로 막는다 (결정 1.11)
> 단일 프로세스 전제(`--workers 1`, 룰 9)에서도 재시작·수동 트리거로 같은 날 브리핑이 두 번
> 나갈 수 있다. `SELECT` 로 "오늘 보냈나?"를 먼저 확인하고 분기하는 방식은 두 실행이 확인과
> INSERT 사이에서 겹치면 그대로 두 번 나간다(TOCTOU). 그래서 판정 자체를 DB 제약에 맡긴다:
>
> ```sql
> INSERT INTO briefing_runs (id, project_id, run_date, sent_at)
> VALUES (:id, :pid, :run_date, now())
> ON CONFLICT (project_id, run_date) DO NOTHING;
> ```
>
> **`rowcount == 1` 일 때만 실제 발송**하고, `0` 이면 이미 누군가 보낸 것이므로 조기 반환한다.
> 같은 설계 근거가 `app/core/scheduler.py:5-10` 에도 적혀 있다 — APScheduler 는 프로세스마다
> 중복 발화하므로 워커를 늘리는 순간 이 제약만이 알림 중복을 막는다.
> *확장 시점의 정답*(범위 밖): 스케줄러 프로세스 분리 + `pg_try_advisory_lock`.

⚠️ `run_date` 는 UTC 날짜가 아니라 **담당자 `users.timezone` 기준 날짜**다 (`04 §2`).
`projects.settings` 에 타임존 키가 없으므로(`04 §3`) 담당자가 교체되면 발송 기준일도 함께
옮겨간다. 그래서 `date` 컬럼이며 timestamptz 가 아니다 — 어느 시각에 보냈는지는 `sent_at`,
어느 "하루"에 대한 발송인지는 `run_date` 로 나눠 둔다.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class BriefingRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "briefing_runs"
    __table_args__ = (
        # `04 §7` — 이 제약이 곧 중복 발송 방지 로직이다 (모듈 독스트링, 결정 1.11).
        UniqueConstraint("project_id", "run_date", name="uq_briefing_runs_project_date"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )

    # 담당자 `users.timezone` 기준 발송 대상 날짜 (`04 §2`).
    run_date: Mapped[date] = mapped_column(Date, nullable=False)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
