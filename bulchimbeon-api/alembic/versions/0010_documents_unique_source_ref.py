"""documents: partial UNIQUE (project_id, source_type, source_ref)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-09 00:40:00.000000

M8 리뷰 패스에서 나온 수정이다 (**사용자 결정 2026-08-08**).

동기화는 "이미 있나?"를 조회한 뒤 INSERT 한다. 담당자가 동기화를 연달아 트리거하면 두 실행이
그 사이에서 겹쳐 **같은 원본 파일이 문서 두 벌**이 된다. 그 뒤로는 조회가 항상 한쪽만 집으므로
나머지 한 벌이 낡은 내용 그대로 `status='active'` 로 남아 근거 검색에 계속 잡힌다 —
룰 6("근거 문서 내용만")이 낡은 근거를 인용하는 형태로 조용히 깨진다.

**락이 아니라 제약으로 막는다** (`briefing_runs` 의 중복 발송 방지, 결정 1.11 과 같은 방식).
동기화는 네트워크 호출을 세션 밖에서 하므로 조회~INSERT 구간에 락을 걸면 원격 응답을 기다리는
동안 DB 커넥션을 붙잡게 된다. 충돌한 쪽은 `IntegrityError` 를 받고 새 문서가 아니라 **새 버전
경로로 재시도**한다 (`services/sync/runner.py`).

업로드 문서는 `source_ref` 가 NULL 이므로 부분 인덱스의 대상이 아니다 — 조건이 없어도 Postgres
에서 NULL 끼리는 충돌하지 않지만, 의도를 인덱스에 남기고 크기도 줄인다.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "uq_documents_source_ref",
        "documents",
        ["project_id", "source_type", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_documents_source_ref", table_name="documents")
