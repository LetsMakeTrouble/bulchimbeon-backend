"""실 LLM 스모크 — M3 DoD (`06 §5` 하단, `prompts/03-question-pipeline.md` 추가 검증).

질문 1건을 **실제 OpenAI 로** 끝까지 돌려 다음을 확인한다:

1. strict Structured Outputs 가 **400 없이** 통과하는가 (`06 §2` ④).
2. `reasoning_effort=minimal` 에서 지연이 데드라인 안인가 (`06 §0` M-1 실측 대조).
3. `1 - (embedding <=> :q)` 로 만든 `sim_raw` 가 실제 임베딩에서도 말이 되는가.

```bash
set -a; . ./.env; set +a
uv run python scripts/smoke_pipeline.py
```

- `OPENAI_API_KEY` 가 없으면 **스킵**한다(종료 코드 0). CI 는 이 스크립트를 돌리지 않는다.
- ⚠️ 실 API 비용이 발생한다. 반복 실행·대량 호출은 하지 않는다 (룰 2 의 유일한 예외가
  이 1회 스모크다).
- 쓴 데이터는 끝에 **전부 롤백**한다 — 개발 DB 에 스모크 흔적을 남기지 않는다.
"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import DEFAULT_SETTINGS, settings  # noqa: E402
from app.database import create_engine  # noqa: E402
from app.models.document import (  # noqa: E402
    INGEST_STATUS_READY,
    SOURCE_TYPE_UPLOAD,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.event import Event  # noqa: E402
from app.models.project import ROLE_ANSWERER, ROLE_ASKER, Project, ProjectMember  # noqa: E402
from app.models.question import Answer, Question  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import event_service  # noqa: E402
from app.services.llm import get_provider  # noqa: E402
from app.services.pipeline import answer as answer_pipeline  # noqa: E402

QUESTION_KO = "환불은 구매 후 며칠 안에 신청해야 하나요?"

# 근거 문서 (영어) — `08 §2` 시드의 환불 정책 청크를 축약한 것.
EVIDENCE = [
    (
        ["Refunds", "Standard Refund Window"],
        "Refunds are accepted within 30 days of purchase. "
        "Requests submitted after 30 days are declined automatically.",
    ),
    (
        ["Refunds", "Partial Refunds"],
        "Partial refunds follow the same 30-day window as full refunds.",
    ),
    (
        ["Sandbox", "Rate Limits"],
        "Sandbox environments are limited to 60 requests per minute per API key.",
    ),
]


async def _seed(db: AsyncSession) -> Question:
    suffix = uuid4().hex[:8]

    answerer = User(
        email=f"smoke-answerer-{suffix}@example.com",
        password_hash="x",
        name="Smoke Answerer",
        language="ko",
        timezone="Asia/Seoul",
    )
    asker = User(
        email=f"smoke-asker-{suffix}@example.com",
        password_hash="x",
        name="Smoke Asker",
        language="ko",
        timezone="Asia/Seoul",
    )
    db.add_all([answerer, asker])
    await db.flush()

    project = Project(
        name=f"smoke-{suffix}",
        invite_code=f"SMOKE{suffix.upper()}",
        answerer_id=answerer.id,
        # ⚠️ DND 창을 닫아 둔다. 열려 있으면 🔴 이 🟡 로 강등돼 스모크의 등급이 흔들린다.
        settings={**DEFAULT_SETTINGS, "dnd_start": "00:00", "dnd_end": "00:00"},
    )
    db.add(project)
    await db.flush()

    db.add_all(
        [
            ProjectMember(project_id=project.id, user_id=answerer.id, role=ROLE_ANSWERER),
            ProjectMember(project_id=project.id, user_id=asker.id, role=ROLE_ASKER),
        ]
    )

    document = Document(
        project_id=project.id, title="Refund Policy", source_type=SOURCE_TYPE_UPLOAD
    )
    db.add(document)
    await db.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_no=1,
        original_filename="refund-policy.md",
        mime="text/markdown",
        storage_path=f"smoke/{suffix}.md",
        is_active=True,
        ingest_status=INGEST_STATUS_READY,
        uploaded_by=answerer.id,
    )
    db.add(version)
    await db.flush()

    # 실 임베딩 1회 — 이 스크립트가 FakeLLM 을 쓰지 않는다는 증거이기도 하다.
    vectors = await get_provider().embed([content for _, content in EVIDENCE])
    for seq, ((heading_path, content), vector) in enumerate(zip(EVIDENCE, vectors, strict=True)):
        db.add(
            Chunk(
                document_version_id=version.id,
                seq=seq,
                content=content,
                meta={"heading_path": heading_path, "page_no": None},
                embedding=vector,
            )
        )

    question = Question(project_id=project.id, asker_id=asker.id, content_ko=QUESTION_KO)
    db.add(question)
    await db.flush()
    return question


async def main() -> int:
    if not settings.openai_api_key:
        print("SKIP: OPENAI_API_KEY 가 비어 있다. `set -a; . ./.env; set +a` 를 먼저 하라.")
        return 0
    if settings.llm_provider != "openai":
        print(f"SKIP: LLM_PROVIDER={settings.llm_provider!r} — 실 LLM 스모크가 아니다.")
        return 0

    engine = create_engine(settings.database_url)
    connection = await engine.connect()
    transaction = await connection.begin()

    def factory() -> AsyncSession:
        return AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )

    original_factory = answer_pipeline.session_factory
    answer_pipeline.session_factory = factory

    try:
        async with factory() as db:
            question = await _seed(db)
            question_id = question.id
            await db.commit()

        print(f"질문: {QUESTION_KO}")
        print(
            f"모델: answer={settings.llm_model_answer} verify={settings.llm_model_verify} "
            f"translate={settings.llm_model_translate} effort={settings.llm_reasoning_effort}"
        )

        await answer_pipeline.run_answer_pipeline(question_id)

        async with factory() as db:
            reloaded = await db.get(Question, question_id)
            answer = await db.scalar(select(Answer).where(Answer.question_id == question_id))
            graded = await db.scalar(
                select(Event).where(
                    Event.entity_id == question_id,
                    Event.type == event_service.EVENT_QUESTION_GRADED,
                )
            )

        if reloaded is None or answer is None or graded is None:
            print("FAIL: 파이프라인이 답변을 남기지 않았다.")
            return 1

        payload = graded.payload
        elapsed_ms = int(payload.get("elapsed_ms") or 0)
        budget_ms = (
            settings.llm_pipeline_deadline_red_seconds
            if answer.grade == "red"
            else settings.llm_pipeline_deadline_seconds
        ) * 1000

        print(f"content_en: {reloaded.content_en}")
        print(
            f"등급: {answer.grade} · 매칭률: {answer.matching_rate} "
            f"(S={answer.search_score} G={answer.grounding_score})"
        )
        print(f"sim_raw(top-1): {answer.sim_raw}")
        print(f"held_reason: {answer.held_reason}")
        print(f"본문(ko): {answer.content_ko}")
        print(f"단계별 elapsed_ms: {payload.get('steps')}")
        print(f"elapsed_ms: {elapsed_ms} / 데드라인 {budget_ms} ({answer.grade} 경로)")

        if elapsed_ms > budget_ms:
            print("FAIL: 데드라인 초과 — M-1 실측(`06 §0`)과 대조하라.")
            return 1

        print("OK: strict 스키마 통과 + 데드라인 이내.")
        return 0
    finally:
        answer_pipeline.session_factory = original_factory
        # 스모크가 개발 DB 에 흔적을 남기지 않는다.
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
