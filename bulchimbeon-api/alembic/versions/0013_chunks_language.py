"""chunks.language: 언어별 검색 축

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-16 02:10:00.000000

질문을 영어 하나로만 번역해 검색하면 한국어 문서가 교차언어 매칭이 되어, 뜻이 같아도
코사인이 크게 떨어진다. 실측(`probe_seed_docs.py`, 2026-08-16): 한국어 청크를 영어 질의로
찾을 때 `백업은 어떤 볼륨들을 함께 받아야 하나요?` 가 **0.1914** 로 `similarity_floor`
아래였다 — 청크 본문이 질문과 거의 같은 문장인데도 강제 🔴 `no_evidence` 가 됐다.

그래서 청크마다 언어를 적어 두고, 질문을 **프로젝트에 실제로 있는 언어들로** 번역해
같은 언어끼리 검색한다.

⚠️ 기존 청크는 `NULL` 로 남는다. 검색은 `NULL` 을 기본 언어(`en`)로 취급하므로 동작이
   깨지지는 않지만, 한국어 문서가 이미 들어 있는 프로젝트는 **다시 인제스트해야** 이
   변경의 효과를 본다 (문서 재업로드 또는 재시드).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("language", sa.Text(), nullable=True))
    # 검색이 `document_version_id IN (...) AND language = :lang` 로 좁힌다.
    # ⚠️ HNSW 는 인덱스 스캔 **이후** WHERE 를 적용하므로(post-filtering) 언어 필터가
    #    붙으면 top-k 가 더 쉽게 0건이 된다. `search_evidence` 의 ef_search 재조회가
    #    그 방어선이고, 이 인덱스는 재조회 경로를 싸게 만든다.
    op.create_index(
        "ix_chunks_version_language",
        "chunks",
        ["document_version_id", "language"],
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_version_language", table_name="chunks")
    op.drop_column("chunks", "language")
