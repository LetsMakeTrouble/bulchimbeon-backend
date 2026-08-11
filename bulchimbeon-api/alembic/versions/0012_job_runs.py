"""job_runs: 예약 작업 실행 기록

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-11 15:10:00.000000

운영 전환 항목 C (`.omc/specs/deep-interview-production-readiness-3.md`).

종전에는 잡이 돌았는지 알 방법이 로그뿐이었고 재시작하면 그마저 사라졌다.
"오늘 아침 브리핑이 왜 안 왔나"에 답할 수 없다는 뜻이다.

⚠️ **이 테이블은 큐가 아니다.** 잡 3종이 전부 상태 파생형이라(만료는 `expires_at`,
좀비는 `processing` 정체, 브리핑은 `briefing_hour` 도달 + 오늘 실행 없음) 무엇을 할지는
DB 상태에서 매번 다시 계산된다. 여기 남기는 것은 할 일이 아니라 **한 일**이다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("job_name", sa.Text(), nullable=False),
        # 기동 직후 1회(`startup`)와 주기 발화(`interval`)를 구분한다 — 배포 직후 밀린
        # 작업이 실제로 따라잡혔는지 보는 신호다.
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # 0 이 정상인 잡이 대부분이라 실패와 구분해서 봐야 한다.
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        # ⚠️ `now()` 가 아니라 `clock_timestamp()` 다 (`04 §7`, M7 실측).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="ck_job_runs_status"),
        sa.CheckConstraint("trigger IN ('interval', 'startup')", name="ck_job_runs_trigger"),
        sa.PrimaryKeyConstraint("id"),
    )
    # "이 잡이 마지막으로 언제 돌았나" 가 유일한 조회 패턴이다.
    op.create_index(
        "ix_job_runs_job_name_started", "job_runs", ["job_name", "started_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_job_runs_job_name_started", table_name="job_runs")
    op.drop_table("job_runs")
