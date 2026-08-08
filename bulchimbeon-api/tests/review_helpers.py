"""M4 확인 워크플로 테스트 헬퍼.

M3 의 `pipeline_helpers` 위에 "카드가 생길 때까지 파이프라인을 태우는" 층을 얹는다 —
M4 의 관심사는 등급 산출이 아니라 **등급이 정해진 다음**이기 때문이다.

> ⚠️ 등급을 만드는 마커 조합은 `fake_provider.py` 독스트링이 정본이다. 여기서는 그 조합에
> 이름만 붙인다 — 마커를 테스트마다 새로 조립하면 어느 등급을 의도했는지 알 수 없게 된다.
"""

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import OfficialQA
from app.models.question import Answer, Question
from app.models.review_card import ReviewCard
from tests.helpers import API, Actor, create_actor, create_project, join_project
from tests.pipeline_helpers import as_uuid, ask, embedding_with_cosine, seed_document

# S = 100 이 되는 원시 코사인 (`s_ceil` 0.679 이상).
HIGH_SIMILARITY = 0.90

# 등급별 마커 (`06 §5` FakeLLM 규약).
GREEN_MARKER = "[[fake:sentences=3,supported=3]]"  # S=100 · G=100 → 🟢
YELLOW_MARKER = "[[fake:sentences=5,supported=3]]"  # S=100 · G=60 → 🟡
RED_MARKER = "[[fake:not_answerable]]"  # 강제 🔴 `no_evidence` + ⑦ 구조화


class Team:
    """담당자 1명 + 질문자 2명 (D1·D17).

    질문자가 둘인 이유는 "맞았다 2건"이 **서로 다른 유저**여야 성립하기 때문이다
    (`04 §7` UNIQUE(answer_id, user_id)).
    """

    def __init__(self, owner: Actor, asker: Actor, asker2: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.asker2 = asker2
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


async def build_team(client: AsyncClient, domain: str) -> Team:
    owner = await create_actor(client, f"owner@{domain}", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, f"asker@{domain}", name="지수", timezone="UTC")
    asker2 = await create_actor(client, f"asker2@{domain}", name="민호", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    await join_project(client, asker2, project["invite_code"])
    return Team(owner, asker, asker2, project)


async def seed_evidence(
    db: AsyncSession,
    team: Team,
    content_ko: str,
    *,
    similarity: float = HIGH_SIMILARITY,
    title: str = "Refund Policy",
) -> tuple[Any, Any, Any]:
    """질문과 `similarity` 만큼 가까운 청크를 심는다. 문서·버전·청크를 돌려준다."""
    result = await seed_document(
        db,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        title=title,
        chunks=[
            (
                "Refunds are accepted within 30 days of purchase.",
                embedding_with_cosine(content_ko, similarity),
            ),
            (
                "Sandbox rate limits apply per minute.",
                embedding_with_cosine(content_ko, max(0.0, similarity - 0.5)),
            ),
        ],
    )
    await db.commit()
    return result


async def seed_new_version(
    db: AsyncSession,
    *,
    document: Any,
    uploader_id: str,
    content: str = "Refunds in Japan follow a 20-day window.",
    version_no: int = 2,
) -> Any:
    """같은 문서의 **새 버전**을 인제스트가 끝난 상태(`ready`)로 심는다.

    활성 전환은 반드시 API(`PATCH .../activate`)로 한다 — 부분 UNIQUE 3문 스왑과 재검토
    연쇄가 그 경로에 있기 때문이다 (`04 §7`).
    """
    from app.models.document import INGEST_STATUS_READY, Chunk, DocumentVersion

    version = DocumentVersion(
        document_id=document.id,
        version_no=version_no,
        original_filename="refund-policy-v2.md",
        mime="text/markdown",
        storage_path=f"{document.project_id}/refund-policy-v2.md",
        is_active=False,
        ingest_status=INGEST_STATUS_READY,
        uploaded_by=as_uuid(uploader_id),
    )
    db.add(version)
    await db.flush()

    db.add(
        Chunk(
            document_version_id=version.id,
            seq=0,
            content=content,
            meta={"heading_path": ["Refunds", "Japan"], "page_no": None},
            embedding=embedding_with_cosine(content, 0.5),
        )
    )
    await db.commit()
    return version


async def ask_until_card(
    client: AsyncClient,
    db: AsyncSession,
    team: Team,
    content_ko: str,
    *,
    urgency: str = "normal",
) -> tuple[str, ReviewCard]:
    """질문 접수 → 파이프라인 완료 → 그 질문의 카드 행을 돌려준다.

    ⚠️ 카드 **행**을 돌려주고 상세 API 를 부르지 않는다 — 상세는 `first_viewed_at` 을 찍으므로
    준비 단계에서 부르면 "아직 안 본 카드" 를 단언하는 테스트가 통째로 무의미해진다 (`05 §7`).
    """
    accepted = await ask(client, team.asker, team.project_id, content_ko, urgency=urgency)
    question_id = accepted["question_id"]

    card = await card_for_question(db, question_id)
    assert card is not None, f"카드가 만들어지지 않았다: question={question_id}"
    return question_id, card


async def card_for_question(
    db: AsyncSession, question_id: str, *, reason: str | None = None
) -> ReviewCard | None:
    stmt = select(ReviewCard).where(ReviewCard.question_id == as_uuid(question_id))
    if reason is not None:
        stmt = stmt.where(ReviewCard.reason == reason)
    return await db.scalar(stmt.order_by(ReviewCard.created_at.desc()).limit(1))


async def cards_for_question(db: AsyncSession, question_id: str) -> list[ReviewCard]:
    rows = await db.scalars(
        select(ReviewCard)
        .where(ReviewCard.question_id == as_uuid(question_id))
        .order_by(ReviewCard.created_at.asc())
    )
    return list(rows.all())


async def list_cards(
    client: AsyncClient, team: Team, *, status: str | None = None
) -> list[dict[str, Any]]:
    params = {"status": status} if status else None
    response = await client.get(
        f"{API}/projects/{team.project_id}/review-cards",
        params=params,
        headers=team.owner.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def card_detail(client: AsyncClient, team: Team, card_id: str) -> dict[str, Any]:
    """⚠️ 이 호출이 `first_viewed_at` 을 찍는다 (`05 §7`)."""
    response = await client.get(f"{API}/review-cards/{card_id}", headers=team.owner.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def act(
    client: AsyncClient,
    team: Team,
    card_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: Actor | None = None,
) -> Any:
    return await client.post(
        f"{API}/review-cards/{card_id}/{action}",
        json=payload,
        headers=(actor or team.owner).headers,
    )


async def feedback(
    client: AsyncClient,
    actor: Actor,
    answer_id: str,
    verdict: str,
    note: str | None = None,
) -> Any:
    body: dict[str, Any] = {"verdict": verdict}
    if note is not None:
        body["note"] = note
    return await client.post(
        f"{API}/answers/{answer_id}/feedback", json=body, headers=actor.headers
    )


async def question_detail(client: AsyncClient, actor: Actor, question_id: str) -> dict[str, Any]:
    response = await client.get(f"{API}/questions/{question_id}", headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def answer_of(db: AsyncSession, question_id: str) -> Answer:
    """⚠️ `expire_all()` 을 부르지 않는다 — `test_pipeline._answer_of` 와 같은 이유다."""
    answer = await db.scalar(select(Answer).where(Answer.question_id == as_uuid(question_id)))
    assert answer is not None, "답변 행이 없다"
    return answer


async def official_qas_of(db: AsyncSession, project_id: str) -> list[OfficialQA]:
    rows = await db.scalars(
        select(OfficialQA)
        .where(OfficialQA.project_id == as_uuid(project_id))
        .order_by(OfficialQA.created_at.asc())
    )
    return list(rows.all())


async def load_question(db: AsyncSession, question_id: str) -> Question:
    question = await db.get(Question, as_uuid(question_id))
    assert question is not None
    return question
