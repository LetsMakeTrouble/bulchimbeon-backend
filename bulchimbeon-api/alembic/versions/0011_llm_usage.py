"""llm_usage: LLM 호출 사용량·비용 적재

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-11 14:34:00.000000

운영 전환 항목 B (`.omc/specs/deep-interview-production-readiness-3.md`).

종전에는 LLM 호출이 **어디에도 남지 않았다.** 호출 수는 `pipeline/quota.py` 의 프로세스
메모리 딕셔너리라 재시작하면 0 이 됐고, 토큰과 비용은 아예 기록되지 않았다. 그 상태에서는
"어떤 계정이 얼마를 썼나"에 답할 수 없고, 일일 상한도 배포 한 번이면 초기화됐다.

`events` 가 아니라 별도 테이블인 이유는 룰 4 의 대상이 **상태 변화**이지 자원 소비가 아니고,
조회 패턴이 다르기 때문이다 — events 는 타임라인(프로젝트+시각), 여기는 집계(주체+기간 SUM).

`cost_usd` 는 **기록 시점 단가로 굳힌다**(`services/llm/pricing.py`). 조회 시점에 다시
계산하면 단가 변경이 과거 비용까지 소급해 "지난달에 얼마 썼나"의 답이 달라진다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 사람이 촉발하지 않은 호출(스케줄러 경유 교훈 추출 등)은 NULL 이다.
        sa.Column("user_id", sa.UUID(), nullable=True),
        # 질문 파이프라인 밖의 호출(확정문 번역·인제스트 임베딩)은 NULL 이다.
        sa.Column("question_id", sa.UUID(), nullable=True),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        # ⚠️ `output_tokens` 에 **포함된** 값이다. 합계에서 따로 더하면 이중 계상이다.
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        # ⚠️ `now()` 가 아니라 `clock_timestamp()` 다 (`04 §7`, M7 실측).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # 일일 한도 판정과 프로젝트 비용 집계가 함께 타는 경로.
    op.create_index(
        "ix_llm_usage_project_created", "llm_usage", ["project_id", "created_at"], unique=False
    )
    # "이 계정이 얼마나 썼나" 조회.
    op.create_index(
        "ix_llm_usage_user_created", "llm_usage", ["user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_project_created", table_name="llm_usage")
    op.drop_table("llm_usage")
