"""job_runs (예약 작업 실행 기록) — 운영 전환 항목 C.

⚠️ 종전에는 **잡이 돌았는지 알 방법이 없었다.** 성공하면 로그 한 줄, 실패하면 예외 로그가
전부였고 그마저 재시작하면 사라졌다. "오늘 아침 브리핑이 왜 안 왔나"에 답할 수 없다는 뜻이다.

> ### 이 테이블은 큐가 아니다
> 잡 3종은 전부 **상태 파생형**이다 — 만료는 `expires_at < now`, 좀비는 `processing` 정체,
> 브리핑은 `briefing_hour 도달 + 오늘 실행 없음`. 무엇을 할지는 DB 상태를 보면 언제든
> 다시 계산되므로 "할 일"을 쌓아 둘 필요가 없고, 그래서 다운타임 이후 **자동으로 소급된다**.
> 여기 남기는 것은 할 일이 아니라 **한 일**이다.
>
> 시각 고정형 작업("X 발생 24시간 뒤 알림")이 생기면 그때는 진짜 큐가 필요하다.
> 지금 만들면 이미 되는 것을 다시 만드는 셈이다.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin

JOB_STATUS_OK = "ok"
JOB_STATUS_FAILED = "failed"
JOB_STATUSES = (JOB_STATUS_OK, JOB_STATUS_FAILED)

# 무엇이 이 실행을 촉발했나. 기동 직후 1회는 주기 발화와 구분돼야 한다 —
# 배포 직후 밀린 작업이 실제로 따라잡혔는지 보는 신호이기 때문이다.
JOB_TRIGGER_INTERVAL = "interval"
JOB_TRIGGER_STARTUP = "startup"
JOB_TRIGGERS = (JOB_TRIGGER_INTERVAL, JOB_TRIGGER_STARTUP)

_STATUS_CHECK = ", ".join(f"'{value}'" for value in JOB_STATUSES)
_TRIGGER_CHECK = ", ".join(f"'{value}'" for value in JOB_TRIGGERS)


class JobRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_CHECK})", name="ck_job_runs_status"),
        CheckConstraint(f"trigger IN ({_TRIGGER_CHECK})", name="ck_job_runs_trigger"),
        # "이 잡이 마지막으로 언제 돌았나" 가 유일한 조회 패턴이다.
        Index("ix_job_runs_job_name_started", "job_name", "started_at"),
    )

    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    # 잡이 실제로 건드린 건수. 0 이 정상인 잡이 대부분이라 실패와 구분해서 봐야 한다.
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
