"""created_at/updated_at default: now() -> clock_timestamp()

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-08 23:10:00.000000

M7 이벤트 기록 감사에서 나온 수정이다.

Postgres 의 `now()` 는 `transaction_timestamp()` 라 **한 트랜잭션 안에서 값이 고정**된다.
요청 하나가 여러 행을 만들면 그 행들의 `created_at` 이 완전히 동일해지고,
`ORDER BY created_at` 이 순서를 만들지 못해 결과가 물리적 행 배치에 따라 달라진다.

실제로 깨지는 곳:
- `05 §13` 타임라인 — 질문 파이프라인 한 번이 `question.graded` · `question.status_changed` ·
  `answer.published` · `card.created` 를 같은 트랜잭션에서 적재한다.
- `05 §7` 큐 목록의 "오래된 순", `card_status_for_question` 의 "그 상태를 만든 카드".

`clock_timestamp()` 는 문장 단위 실시각이라 INSERT 순서가 그대로 시각 순서가 된다.
스키마(컬럼·타입·인덱스·제약)는 그대로이고 **기본값 표현식만** 바뀐다.
근거는 `app/models/base.py` 독스트링.

⚠️ 기존 행은 손대지 않는다 — 과거 데이터의 시각을 사후에 바꾸면 지표가 소급 변조된다.
   이 마이그레이션 이전에 쌓인 같은-트랜잭션 행들은 계속 동률이며, 그건 기록된 사실이다.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `models/base.py` 의 `CreatedAtMixin` / `TimestampMixin` 을 쓰는 전 테이블.
# `chunks` · `events` 는 append-only 라 `created_at` 만 있다.
_CREATED_AT_ONLY = ("chunks", "events")
_CREATED_AND_UPDATED = (
    "answer_citations",
    "answers",
    "briefing_runs",
    "document_versions",
    "documents",
    "feedbacks",
    "guidelines",
    "lessons",
    "notifications",
    "official_qas",
    "project_members",
    "projects",
    "questions",
    "review_cards",
    "users",
)


def _set_default(expression: str) -> None:
    for table in _CREATED_AT_ONLY:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.text(expression),
        )
    for table in _CREATED_AND_UPDATED:
        for column in ("created_at", "updated_at"):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=sa.text(expression),
            )


def upgrade() -> None:
    """Upgrade schema."""
    _set_default("clock_timestamp()")


def downgrade() -> None:
    """Downgrade schema."""
    _set_default("now()")
