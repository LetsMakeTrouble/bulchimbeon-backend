"""동시 파이프라인 상한 (운영 전환 항목 A).

⚠️ **커넥션 풀 고갈 자체는 여기서 재현할 수 없다.** 테스트는 커넥션 하나를 공유하며
롤백으로 격리하기 때문이다(`03 §5.3`). 풀 고갈 재현은 `scripts/probe_concurrency.py` 가
실제 서버에 대고 한다. 여기서 지키는 것은 **상한이 실제로 걸리는가**와
**대기가 데드라인을 먹지 않는가** 둘이다.
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.event import Event
from app.models.user import User
from app.schemas.question import QuestionCreate
from app.services import event_service, question_service
from app.services.pipeline import concurrency
from app.services.pipeline.answer import run_answer_pipeline
from tests.helpers import Actor, create_actor, create_project, join_project
from tests.pipeline_helpers import as_uuid, embedding_with_cosine, seed_document
from tests.review_helpers import HIGH_SIMILARITY

# 슬롯을 붙잡아 두는 시간. fake 파이프라인 한 건은 수십 ms 라, 이 값이 `elapsed_ms` 에
# 섞이면 즉시 드러난다.
QUEUE_WAIT_SECONDS = 0.6


class Team:
    def __init__(self, owner: Actor, asker: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


@pytest.fixture
async def team(client: AsyncClient) -> Team:
    owner = await create_actor(client, "owner@conc.test", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@conc.test", name="질문자", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return Team(owner, asker, project)


@pytest.fixture(autouse=True)
def isolated_semaphore() -> Any:
    """세마포어는 이벤트 루프별 캐시라 상한을 바꾸면 비워야 한다."""
    concurrency.reset()
    yield
    concurrency.reset()


def test_limit_comes_from_settings_not_a_hardcoded_number() -> None:
    """상한은 `config` 한 곳에서 온다. 풀 크기와 **함께** 움직여야 하기 때문이다."""
    assert settings.pipeline_max_concurrency >= 1
    # 풀에 요청 처리용 여유가 남아야 한다 — 상한을 두는 이유가 그것이다.
    assert settings.db_pool_size + settings.db_max_overflow > settings.pipeline_max_concurrency


async def test_slot_admits_only_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """상한을 넘긴 요청은 **실패가 아니라 대기**한다."""
    monkeypatch.setattr(settings, "pipeline_max_concurrency", 2)
    concurrency.reset()

    inside = 0
    peak = 0
    release = asyncio.Event()

    async def worker() -> None:
        nonlocal inside, peak
        async with concurrency.pipeline_slot():
            inside += 1
            peak = max(peak, inside)
            await release.wait()
            inside -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(5)]
    await asyncio.sleep(0.05)
    assert peak == 2, f"상한 2 인데 동시에 {peak} 건이 들어갔다"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


async def test_queue_wait_does_not_count_against_the_deadline(
    db_session: AsyncSession,
    team: Team,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ 사양: **슬롯 → 세션 → 시계** 순서다.

    줄을 선 시간이 `elapsed_ms` 에 포함되면 "붐볐다"는 이유로 데드라인을 넘겨 멀쩡한
    답변이 강등된다. 여기서는 테스트가 슬롯을 **직접 붙잡아** 파이프라인을 줄 세운 뒤,
    풀려난 파이프라인의 `elapsed_ms` 가 대기 시간을 담고 있지 않은지 본다.

    ⚠️ 파이프라인을 여러 개 **동시에** 돌리는 방식으로는 못 잰다 — 테스트가 커넥션 하나를
    공유하고 세션마다 savepoint 를 여는 구조라(`03 §5.3`) 중첩 savepoint 에서 죽는다.
    슬롯을 밖에서 잡으면 대기 중인 파이프라인이 세션을 아직 안 열었으므로 그 충돌이 없다 —
    **그 사실 자체가 순서가 지켜졌다는 증거**이기도 하다.
    """
    monkeypatch.setattr(settings, "pipeline_max_concurrency", 1)
    concurrency.reset()

    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=1,supported=1]]"
    await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[
            (
                "Refunds are accepted within 30 days of purchase.",
                embedding_with_cosine(content_ko, HIGH_SIMILARITY),
            )
        ],
    )
    asker = await db_session.get(User, as_uuid(team.asker.id))
    assert asker is not None
    question = await question_service.create_question(
        db_session,
        project_id=as_uuid(team.project_id),
        asker=asker,
        payload=QuestionCreate(content_ko=content_ko, urgency="normal"),
    )
    question_id = question.id
    await db_session.commit()

    async with concurrency.pipeline_slot():
        task = asyncio.create_task(run_answer_pipeline(question_id))
        await asyncio.sleep(QUEUE_WAIT_SECONDS)
        assert not task.done(), "슬롯을 붙잡고 있는데 파이프라인이 그냥 지나갔다"
    await task

    payload = await _graded_payload(db_session, str(question_id))
    assert payload is not None, "파이프라인이 끝나지 않았다"

    waited_ms = QUEUE_WAIT_SECONDS * 1000
    assert payload["elapsed_ms"] < waited_ms, (
        f"대기 {waited_ms:.0f}ms 가 시계에 들어갔다 (elapsed_ms={payload['elapsed_ms']}) — "
        "슬롯 획득이 컨텍스트 생성보다 뒤로 갔는지 확인하라"
    )


async def _graded_payload(db: AsyncSession, question_id: str) -> dict[str, Any] | None:
    """`question.graded` 페이로드. 아직 안 끝났으면 None."""
    event = await db.scalar(
        select(Event).where(
            Event.entity_id == as_uuid(question_id),
            Event.type == event_service.EVENT_QUESTION_GRADED,
        )
    )
    return dict(event.payload) if event is not None else None
