"""질문 파이프라인 DoD — `06 §5` 테스트 1·2·3·5·6·7 + 추가 검증.

테스트 4(재사용 경계)는 `test_reuse.py` 에 있다.

> 등급 분기를 보려면 `sim_raw` 를 통제해야 한다. `pipeline_helpers.embedding_with_cosine`
> 이 목표 코사인을 갖는 벡터를 합성해 청크에 심는다 — 그래서 이 테스트들은
> `1 - (embedding <=> :q)` 라는 **SQL 산출식 자체**를 함께 검증한다.
"""

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.event import Event
from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_EXPIRED,
    ANSWER_STATE_REJECTED,
    ANSWER_STATE_UNDER_REVIEW,
    Answer,
    AnswerCitation,
    Question,
)
from app.services import answer_service, event_service
from app.services.llm.fake_provider import SENTENCE_BLOCK_PREFIX, FakeLLMProvider
from app.services.pipeline import prompts, quota
from app.services.pipeline.answer import _citation_quote
from tests.helpers import (
    Actor,
    close_dnd_window,
    create_actor,
    create_project,
    error_code,
    join_project,
)
from tests.pipeline_helpers import (
    as_uuid,
    ask,
    ask_and_get,
    embedding_with_cosine,
    patch_project_settings,
    seed_document,
    set_away_mode,
)

# S = 100 이 되는 원시 코사인 (`s_ceil` 0.679 이상).
HIGH_SIMILARITY = 0.90
# `similarity_floor` 미만 — 청크가 반환돼도 강제 🔴 `no_evidence` 다.
# ⚠️ 숫자를 주석에 박지 마라 — 값은 `DEFAULT_SETTINGS` 에 있고
#    2026-08-09 에 0.423 → 0.444 로 움직였다.
BELOW_FLOOR_SIMILARITY = 0.30


class Fixture:
    def __init__(self, owner: Actor, asker: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Fixture:
    """담당자(프로젝트 생성자) 1명 + 질문자 1명 (D1·D17)."""
    owner = await create_actor(client, "owner@pipeline.test", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@pipeline.test", name="질문자", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    # DND 를 꺼 둔다 — 이유는 `helpers.close_dnd_window` 참조. 이 파일의 DND 테스트들은
    # 그 뒤에 자기 창을 명시적으로 덮어쓴다.
    await close_dnd_window(client, owner, project["id"])
    return Fixture(owner, asker, project)


async def _seed_evidence(
    db: AsyncSession,
    team: Fixture,
    content_ko: str,
    *,
    similarity: float = HIGH_SIMILARITY,
    extra_chunks: int = 1,
) -> None:
    """질문과 `similarity` 만큼 가까운 청크 1개 + 무관한 청크 몇 개를 심는다."""
    chunks = [
        (
            "Refunds are accepted within 30 days of purchase.",
            embedding_with_cosine(content_ko, similarity),
        )
    ]
    for index in range(extra_chunks):
        chunks.append(
            (
                f"Unrelated paragraph {index}: sandbox rate limits apply per minute.",
                embedding_with_cosine(content_ko, max(0.0, similarity - 0.4 - 0.05 * index)),
            )
        )

    await seed_document(
        db,
        project_id=team.project["id"],
        uploader_id=team.owner.id,
        chunks=chunks,
    )
    await db.commit()


async def _answer_of(db: AsyncSession, question_id: str) -> Answer:
    """⚠️ 여기서 `expire_all()` 을 부르지 않는다.

    파이프라인은 **다른 세션**에서 돌지만 같은 커넥션을 공유하므로 새 행은 그냥 보인다.
    반면 `expire_all()` 은 이미 돌려준 객체까지 만료시켜, 뒤이은 속성 접근이 동기 IO 를
    시도하다 `MissingGreenlet` 으로 죽는다. 만료가 필요한 것은 **테스트 세션이 직접 적재한**
    객체(예: 라우터가 만든 `Question`)뿐이다.
    """
    answer = await db.scalar(select(Answer).where(Answer.question_id == as_uuid(question_id)))
    assert answer is not None, "답변 행이 없다 — 파이프라인이 발행하지 않았다"
    return answer


async def _events(db: AsyncSession, question_id: str, type_: str) -> list[Event]:
    rows = await db.scalars(
        select(Event).where(Event.entity_id == as_uuid(question_id), Event.type == type_)
    )
    return list(rows)


async def _graded_payload(db: AsyncSession, question_id: str) -> dict[str, Any]:
    events = await _events(db, question_id, event_service.EVENT_QUESTION_GRADED)
    assert len(events) == 1, f"question.graded 는 정확히 1회여야 한다 (실제 {len(events)})"
    return events[0].payload


# --------------------------------------------------------------------------------------
# 테스트 1 — 리스케일 + min(S, G)
# --------------------------------------------------------------------------------------
async def test_high_s_high_g_is_green(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=3,supported=3]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "answered"
    assert detail["answer"]["grade"] == "green"
    assert detail["answer"]["search_score"] == 100
    assert detail["answer"]["grounding_score"] == 100
    assert detail["answer"]["matching_rate"] == 100
    # 🟢 도 `draft` 로 시작한다 — 자동 확정은 어떤 경로에도 없다 (룰 3).
    assert detail["answer"]["state"] == ANSWER_STATE_DRAFT
    assert detail["answer"]["disclaimer"]


async def test_high_s_low_g_is_downgraded_by_min_rule(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """룰 1 의 존재 이유 — 문서를 잘 찾아도 답변이 문서를 벗어나면 낮은 등급이다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=5,supported=3]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = detail["answer"]

    assert answer["search_score"] == 100, "S 는 높다"
    assert answer["grounding_score"] == 60, "G = 3/5"
    # 평균(80)이면 🟢 이 됐을 것이다. min 이라서 🟡 이다.
    assert answer["matching_rate"] == 60
    assert answer["grade"] == "yellow"


async def test_search_score_follows_the_rescale_formula(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """S = round(clamp((sim_raw - s_floor)/(s_ceil - s_floor), 0, 1) × 100)."""
    similarity = 0.50
    content_ko = f"부분 환불도 같은 기한인가요? [[fake:sentences=2,supported=2]] {similarity}"
    await _seed_evidence(db_session, team, content_ko, similarity=similarity)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    s_floor = DEFAULT_SETTINGS["s_floor"]
    s_ceil = DEFAULT_SETTINGS["s_ceil"]
    expected = round((similarity - s_floor) / (s_ceil - s_floor) * 100)

    assert answer.sim_raw == pytest.approx(similarity, abs=1e-5)
    assert answer.search_score == expected


# --------------------------------------------------------------------------------------
# 테스트 2 — G 분모 고정
# --------------------------------------------------------------------------------------
async def test_pruning_does_not_raise_the_matching_rate(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """5문장 중 2문장만 supported → 3문장을 지워도 **G_final = 40** 이다.

    분모를 프루닝 후 개수로 쓰면 G=100 이 되어 환각이 심할수록 등급이 올라간다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=5,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])
    payload = await _graded_payload(db_session, accepted["question_id"])

    assert answer.grounding_score == 40, "분모는 생성 시점 원본 문장 수(5)로 고정이다"
    assert answer.matching_rate == 40
    assert payload["G_raw"] == 40
    assert payload["G_final"] == 40
    assert len(payload["removed_sentences"]) == 3

    # 발행 본문에는 supported 2문장만 남는다 (프루닝은 발행 품질을 위한 조치다).
    assert answer.content_en.count("Supported sentence") == 2
    assert "Unsupported" not in answer.content_en

    # min(S=100, G=40) = 40 < yellow_threshold → 🔴 low_confidence
    assert answer.grade == "red"
    assert answer.held_reason == "low_confidence"


async def test_all_sentences_removed_is_red(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=3,supported=0]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "held"
    assert detail["answer"] is None
    assert detail["held_info"]["reason"] == "low_confidence"


async def test_two_sentence_answer_requires_every_sentence_supported(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """1~2문장 구간은 `grounding_min` 을 적용하지 않는다 — 하나라도 무근거면 🔴.

    ⚠️ 이 케이스는 min(S,G) 만으로는 `min(100, 50) = 50 ≥ yellow_threshold` 라서 🟡 이다.
    🔴 이 나온다는 것이 곧 이 규칙이 살아 있다는 증거다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=1]]"
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    assert answer.grounding_score == 50
    assert answer.grade == "red"
    assert answer.held_reason == "low_confidence"


async def test_three_sentence_answer_uses_grounding_min(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """문장 3개부터는 비율 규칙이 산다 — G=67 이면 프루닝 없이 발행된다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=3,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    assert answer.grounding_score == 67
    assert answer.grade == "yellow"
    # 프루닝 조건(G < 60)에 걸리지 않았으므로 무근거 문장도 그대로 실린다.
    assert "Unsupported sentence 3." in answer.content_en


# --------------------------------------------------------------------------------------
# 테스트 3 — conflict 강제 🔴 + question_struct
# --------------------------------------------------------------------------------------
async def test_conflict_is_forced_red_and_stores_question_struct(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "일본 리전도 같은 환불 기한인가요? [[fake:conflict]]"
    await _seed_evidence(db_session, team, content_ko, extra_chunks=2)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, detail["id"])

    assert detail["status"] == "held"
    assert detail["answer"] is None, "🔴 은 질문자에게 발행되지 않는다"
    assert detail["held_info"]["reason"] == "conflict"
    assert detail["held_info"]["card_status"] == "pending"

    # 초안은 DB 에 남는다 — 카드가 담당자에게 보여준다 (`04 §2`).
    assert answer.grade == "red"
    assert answer.state == ANSWER_STATE_DRAFT

    # ⑦ 결과는 `answers.question_struct` 에 저장된다. M4 가 카드로 복사한다.
    assert answer.question_struct is not None
    assert set(answer.question_struct) == {"background", "question", "options"}
    assert len(answer.question_struct["options"]) >= 2


# --------------------------------------------------------------------------------------
# 강제 🔴 — no_evidence
# --------------------------------------------------------------------------------------
async def test_zero_search_result_is_no_evidence(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """문서가 하나도 없으면 강제 🔴 `no_evidence` 다 (일반 상식 폴백 금지 — 룰 6)."""
    content_ko = "환불 기한이 며칠인가요?"

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "no_evidence"


async def test_similarity_floor_forces_no_evidence(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """청크가 반환돼도 top-1 `sim_raw < similarity_floor` 면 강제 🔴 다.

    벡터 검색은 항상 "가장 가까운 무언가"를 돌려주므로 하한이 없으면 무관한 청크로 답을 만든다.
    """
    content_ko = "환불 기한이 며칠인가요?"
    await _seed_evidence(db_session, team, content_ko, similarity=BELOW_FLOOR_SIMILARITY)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    assert answer.sim_raw == pytest.approx(BELOW_FLOOR_SIMILARITY, abs=1e-5)
    assert answer.sim_raw < DEFAULT_SETTINGS["similarity_floor"]
    assert answer.grade == "red"
    assert answer.held_reason == "no_evidence"


async def test_not_answerable_is_no_evidence(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """ "주제만 언급한 청크는 근거가 아니다" — ④ 가 `not_answerable` 을 세우는 경로."""
    content_ko = "레이트 리밋은 분당 몇 건인가요? [[fake:not_answerable]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["held_info"]["reason"] == "no_evidence"


# --------------------------------------------------------------------------------------
# 인용 id 실재 검증 (환각 방어 1겹)
# --------------------------------------------------------------------------------------
async def test_citation_id_not_in_evidence_makes_the_sentence_unsupported(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`[EVIDENCE]` 에 없는 id 는 제거되고 그 문장은 `supported=false` 로 확정된다.

    ⑤ 를 거치지 않고 서버가 직접 떨어뜨리므로, 모델이 id 를 지어내면 G 가 즉시 내려간다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=4,supported=4,ghost_citation]]"
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    # 4문장 중 마지막은 존재하지 않는 별칭을 인용했다 → 3/4.
    assert answer.grounding_score == 75


# --------------------------------------------------------------------------------------
# 테스트 5 — DND 분기
# --------------------------------------------------------------------------------------
async def test_dnd_degrades_only_low_confidence(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`low_confidence` + 발행할 문장 있음 → 🟡 강등 (룰 6, D2)."""
    await patch_project_settings(db_session, team.project_id, dnd_start="00:00", dnd_end="23:59")
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=5,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, detail["id"])

    assert detail["status"] == "answered"
    assert detail["answer"]["grade"] == "yellow"
    assert detail["answer"]["degraded_from_red"] is True
    # 강등된 답변에 held_reason 이 남으면 "보류였다가 담당자가 답한 질문"과 구분되지 않는다.
    assert detail["held_info"] is None
    assert answer.held_reason is None


async def test_dnd_does_not_degrade_forced_red(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """강제 🔴 4종은 **DND 에서도 🔴 을 유지**한다 (D2).

    이 규칙 덕분에 데모 Q7·Q8 이 시연 시각과 무관하게 🔴 로 재현된다.
    """
    await patch_project_settings(db_session, team.project_id, dnd_start="00:00", dnd_end="23:59")
    content_ko = "일본 리전도 같은 환불 기한인가요? [[fake:conflict]]"
    await _seed_evidence(db_session, team, content_ko, extra_chunks=2)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, detail["id"])

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "conflict"
    assert answer.degraded_from_red is False


async def test_dnd_cannot_degrade_when_nothing_is_left_to_publish(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """강등은 **제거 후 남는 문장이 있을 때만** 가능하다 (룰 6 구현 노트)."""
    await patch_project_settings(db_session, team.project_id, dnd_start="00:00", dnd_end="23:59")
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=3,supported=0]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "low_confidence"


async def test_away_mode_shares_the_degrade_gate(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """퇴근 모드도 DND 와 같은 강등 관문을 쓴다 (`05 §6` "퇴근 모드/DND 시간대").

    DND 창을 닫아 두었으므로(`00:00`~`00:00`) 강등을 일으킨 것은 `away_mode` 뿐이다.
    """
    await patch_project_settings(db_session, team.project_id, dnd_start="00:00", dnd_end="00:00")
    await set_away_mode(db_session, team.project_id, True)

    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=5,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["answer"]["grade"] == "yellow"
    assert detail["answer"]["degraded_from_red"] is True


# --------------------------------------------------------------------------------------
# 테스트 6 — 스키마 3회 실패 (FakeLLM 전용 경로)
# --------------------------------------------------------------------------------------
async def test_schema_failure_exhausts_retries_and_holds(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """⚠️ **FakeLLM 전용 경로**다. 실 API strict 모드에서는 refusal·max token 초과가 아니면
    도달하지 않는다 (`06 §2` ④).
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:schema_fail]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, detail["id"])

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "schema_failed"
    # 스키마 실패도 담당자에게 넘어가야 하므로 ⑦ 구조화는 그대로 돈다.
    assert answer.question_struct is not None


# --------------------------------------------------------------------------------------
# 강제 🔴 — quota_exceeded
# --------------------------------------------------------------------------------------
async def test_quota_exceeded_is_forced_red_without_any_llm_call(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, fake_llm_provider: Any
) -> None:
    """한도를 넘기면 LLM 을 한 번도 부르지 않고 🔴 이다 (`06 §6`)."""
    await patch_project_settings(db_session, team.project_id, daily_llm_call_limit=3)
    await quota.set_used(db_session, as_uuid(team.project_id), 3)
    before = len(fake_llm_provider.complete_json_calls)

    content_ko = "환불 기한이 며칠인가요?"
    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, detail["id"])

    assert detail["held_info"]["reason"] == "quota_exceeded"
    assert len(fake_llm_provider.complete_json_calls) == before
    # ⑦ 구조화도 LLM 호출이므로 건너뛴다.
    assert answer.question_struct is None


async def test_quota_exceeded_stays_red_under_dnd(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    await patch_project_settings(
        db_session,
        team.project_id,
        daily_llm_call_limit=1,
        dnd_start="00:00",
        dnd_end="23:59",
    )
    await quota.set_used(db_session, as_uuid(team.project_id), 5)

    detail = await ask_and_get(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "quota_exceeded"


# --------------------------------------------------------------------------------------
# 테스트 7 — 만료 답변은 확정 대상에서 제외된다 (D13, 서비스 계층 가드)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state", [ANSWER_STATE_EXPIRED, ANSWER_STATE_REJECTED, ANSWER_STATE_UNDER_REVIEW]
)
def test_feedback_is_rejected_outside_draft_and_verified(state: str) -> None:
    """D12 — 허용 상태는 `draft` · `verified` 뿐이다."""
    from app.core.errors import FeedbackNotAllowed

    answer = Answer(grade="yellow", state=state)
    with pytest.raises(FeedbackNotAllowed):
        answer_service.ensure_feedback_allowed(answer)


def test_expired_answer_cannot_be_confirmed() -> None:
    """`expired` 는 **종착 상태**다 — 승인·수정 대상에서 제외된다 (D13).

    e2e(카드 처리 → 409)는 M4 에서 완성한다.
    """
    from app.core.errors import InvalidCardAction

    expired = Answer(grade="yellow", state=ANSWER_STATE_EXPIRED)
    with pytest.raises(InvalidCardAction):
        answer_service.ensure_confirmable(expired)

    draft = Answer(grade="yellow", state=ANSWER_STATE_DRAFT)
    answer_service.ensure_confirmable(draft)  # 예외가 없어야 한다


# --------------------------------------------------------------------------------------
# `1 - (<=>)` 회귀 — FakeLLM 만으로는 절대 검출되지 않는 결함
# --------------------------------------------------------------------------------------
async def test_identical_vector_yields_similarity_near_one(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """동일 벡터의 `sim_raw` 가 **1.0 에 가깝게** 나오는지 SQL 레벨로 확인한다.

    `<=>` 는 거리다. 부호가 뒤집혀 있으면(거리를 유사도로 쓰면) 여기서 0 이 나온다 —
    등급이 정확히 반대로 매겨지는 결함을 잡는 유일한 관문이다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko, similarity=1.0)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await _answer_of(db_session, accepted["question_id"])

    assert answer.sim_raw == pytest.approx(1.0, abs=1e-5)
    assert answer.search_score == 100


# --------------------------------------------------------------------------------------
# BackgroundTasks 규약
# --------------------------------------------------------------------------------------
async def test_question_never_stays_in_processing(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """UUID 만 넘기는 규약이 지켜지면 응답 사이클 안에서 파이프라인이 끝난다.

    ORM 객체·세션을 넘기면 태스크가 이미 닫힌 세션을 만져 질문이 `processing` 에 영구 정지한다.
    """
    content_ko = "환불 기한이 며칠인가요?"
    accepted = await ask(client, team.asker, team.project_id, content_ko)

    assert accepted["status"] == "processing"

    db_session.expire_all()
    question = await db_session.get(Question, as_uuid(accepted["question_id"]))
    assert question is not None
    assert question.status != "processing"


async def test_pipeline_failure_marks_question_failed(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """총 실패도 질문을 잃지 않는다 (D23). 실패 기록은 **새 세션**에서 커밋된다."""
    from app.services.pipeline import answer as answer_pipeline

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("의도적 실패")

    monkeypatch.setattr(answer_pipeline, "_pipeline", _boom)

    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    db_session.expire_all()
    question = await db_session.get(Question, as_uuid(accepted["question_id"]))
    assert question is not None
    assert question.status == "failed"

    detail = await client.get(
        f"/api/v1/questions/{accepted['question_id']}", headers=team.asker.headers
    )
    body = detail.json()
    assert body["answer"] is None
    assert body["failure_info"]["reason"] == "pipeline_error"
    assert body["failure_info"]["message"]


# --------------------------------------------------------------------------------------
# 데드라인 안전망 (`06 §6`)
# --------------------------------------------------------------------------------------
async def test_deadline_after_generation_publishes_yellow_without_a_grounding_score(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """데드라인 초과 시 **그 시점까지의 결과로 🟡 발행**한다 (안전망).

    ⑤ 를 돌리지 못했으므로 G 를 100 으로 채우지 않는다 — 검증하지 않은 근거를 주장하는 셈이다.
    `grounding_score` 와 `matching_rate` 는 NULL 로 두고 증적에 `deadline_exceeded` 를 남긴다.
    """
    from app.services.pipeline import answer as answer_pipeline

    def _over(self: Any, *, red_path: bool = False) -> bool:
        # ④ 가 끝난 뒤부터 초과로 본다 — 그래야 "발행할 결과는 있는" 구간이 재현된다.
        return any(name == "generate" for name, _ in self.steps)

    monkeypatch.setattr(answer_pipeline._Ctx, "over_deadline", _over)

    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=3,supported=3]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    payload = await _graded_payload(db_session, detail["id"])

    assert detail["status"] == "answered"
    assert detail["answer"]["grade"] == "yellow"
    assert detail["answer"]["grounding_score"] is None
    assert detail["answer"]["matching_rate"] is None
    assert detail["answer"]["search_score"] == 100
    assert payload["deadline_exceeded"] is True
    # ⑤ 는 호출되지 않았다.
    assert "verify" not in dict(payload["steps"])


async def test_deadline_before_any_result_fails_the_question(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """결과가 아예 없으면 🟡 로 내보낼 것이 없다 → `status='failed'` (D23)."""
    from app.services.pipeline import answer as answer_pipeline

    monkeypatch.setattr(answer_pipeline._Ctx, "over_deadline", lambda self, *, red_path=False: True)

    content_ko = "환불 기한이 며칠인가요?"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "failed"
    assert detail["answer"] is None
    assert detail["failure_info"]["card_status"] == "pending"


# --------------------------------------------------------------------------------------
# 이벤트 증적 (`04 §5`)
# --------------------------------------------------------------------------------------
async def test_graded_event_records_every_evidence_field(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`S_raw`·`S`·`G_raw`·`G_final`·`removed_sentences` 를 **전부** 남긴다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=5,supported=3]]"
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    payload = await _graded_payload(db_session, accepted["question_id"])

    for key in ("S_raw", "S", "G_raw", "G_final", "removed_sentences", "grade", "matching_rate"):
        assert key in payload, f"{key} 가 증적에서 빠졌다"
    assert payload["elapsed_ms"] >= 0
    assert payload["source"] == "generated"


async def test_status_changed_event_carries_from_and_to(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:conflict]]"
    await _seed_evidence(db_session, team, content_ko, extra_chunks=2)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    events = await _events(
        db_session, accepted["question_id"], event_service.EVENT_QUESTION_STATUS_CHANGED
    )

    assert len(events) == 1
    assert events[0].payload == {"from": "processing", "to": "held"}


async def test_citations_expose_document_and_version_ids(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`05 §6` — 두 필드 없이는 근거 원문 열람 URL 을 만들 수 없다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    citations = detail["answer"]["citations"]

    assert citations, "인용이 비어 있다"
    for citation in citations:
        assert citation["document_id"]
        assert citation["document_version_id"]
        assert citation["heading_path"] == ["Refunds", "Standard"]
        assert citation["page_no"] is None
        assert 0.0 <= citation["similarity"] <= 1.0

    db_session.expire_all()
    rows = await db_session.scalars(
        select(AnswerCitation).where(AnswerCitation.answer_id == as_uuid(detail["answer"]["id"]))
    )
    # D24 — 공식 Q&A 는 인용되지 않는다.
    assert all(row.official_qa_id is None for row in rows)


async def test_deleted_document_chunks_leave_the_search_scope(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """soft delete 된 문서의 청크는 물리 삭제하지 않되 검색에서 빠진다 (D20)."""
    from app.models.document import DOCUMENT_STATUS_DELETED

    content_ko = "환불 기한이 며칠인가요?"
    await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[("Refunds are accepted within 30 days.", embedding_with_cosine(content_ko, 0.9))],
        document_status=DOCUMENT_STATUS_DELETED,
    )
    await db_session.commit()

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["held_info"]["reason"] == "no_evidence"


async def test_answerer_cannot_ask_in_own_project(client: AsyncClient, team: Fixture) -> None:
    """D17 — 담당자는 자기 프로젝트에 질문할 수 없다."""
    response = await client.post(
        f"/api/v1/projects/{team.project_id}/questions",
        json={"content_ko": "내 프로젝트에 질문할 수 있나?"},
        headers=team.owner.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_verify_receives_the_whole_chunk_not_a_display_snippet(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Fixture,
    fake_llm_provider: FakeLLMProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑤ 는 인용 청크를 **자르지 않고** 받는다 (`06 §2` ⑤).

    `QUOTE_MAX_LENGTH` 는 화면 하이라이트용 스니펫 길이(`05 §6` `citations[].quote`)다.
    그 값으로 ⑤ 의 근거를 자르면 ④ 는 청크 전문을 보고 문장을 쓰는데 ⑤ 는 앞부분만 보고
    판정하게 되어, 근거가 청크 **뒤쪽**에 있는 문장이 통째로 "근거 없음"이 된다.
    배포본 실측에서 🔴 `low_confidence` 10건이 전부 이 경로였고 G 가 예외 없이 0 이었다.
    """
    content_ko = "GET 요청에도 멱등키가 적용되나요? [[fake:sentences=1,supported=1]]"
    tail = "Idempotency keys are ignored on GET and DELETE requests."
    body = (
        "Write endpoints accept an optional Idempotency-Key header. "
        + "The key is stored with the request fingerprint for 24 hours. " * 8
        + tail
    )
    # 잘림이 실제로 일어나는 길이가 아니면 이 테스트는 아무것도 지키지 못한다.
    assert len(body) > prompts.QUOTE_MAX_LENGTH
    assert body.index(tail) > prompts.QUOTE_MAX_LENGTH

    await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[(body, embedding_with_cosine(content_ko, HIGH_SIMILARITY))],
    )
    await db_session.commit()

    seen: list[str] = []
    original = fake_llm_provider.complete_json

    async def recording(
        system: str, user: str, schema: Any, *, model: str | None = None, step: str = "unknown"
    ) -> Any:
        seen.append(user)
        return await original(system, user, schema, model=model, step=step)

    monkeypatch.setattr(fake_llm_provider, "complete_json", recording)

    await ask_and_get(client, team.asker, team.project_id, content_ko)

    verify_prompts = [user for user in seen if SENTENCE_BLOCK_PREFIX in user]
    assert verify_prompts, "⑤ 가 호출되지 않았다 — 이 테스트의 전제가 깨졌다"
    assert tail in verify_prompts[0], (
        "인용 청크가 잘려 ⑤ 가 근거 뒷부분을 보지 못했다 — G 가 0 으로 무너지는 경로다"
    )


def test_citation_quote_takes_the_verified_span_only_when_it_exists_in_the_chunk() -> None:
    """⑤ 가 지목한 인용문은 **청크에 실재할 때만** 쓴다 (`06 §7` 환각 방어).

    실재 검증이 무너지면 지어낸 문장이 근거로 저장되고, 프론트는 원문에서 그 문자열을
    찾지 못해 하이라이트가 통째로 사라진다 (`05 §6`). 폴백은 종전 동작(청크 앞부분)이다.
    """
    content = "Refunds are issued within 30 days. Shipping fees are not refunded."
    picked = "Shipping fees are not refunded."

    assert _citation_quote(picked, content) == picked
    # 앞뒤 공백은 인용의 일부가 아니다 — 이것 때문에 폴백으로 떨어지면 손해만 본다.
    assert _citation_quote(f"  {picked}\n", content) == picked
    assert _citation_quote("Refunds take 90 days.", content) == content  # 환각 → 폴백
    assert _citation_quote("", content) == content  # ⑤ 미실행·근거 없음 → 폴백

    # ⚠️ 잘라내기는 실재 검증 **뒤**다. 순서가 뒤집히면 500자를 넘는 정당한 인용이
    #    전부 폴백으로 떨어져 이 기능이 조용히 죽는다.
    long_content = "Idempotency. " * prompts.QUOTE_MAX_LENGTH
    assert _citation_quote(long_content, long_content) == long_content[: prompts.QUOTE_MAX_LENGTH]


async def test_citation_quote_is_the_supporting_sentence_not_the_chunk_head(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`05 §6` `citations[].quote` — 출처를 가리키는 데서 그치지 않고 근거 문장을 인용한다.

    ⑤ 는 판정하려고 **이미** 근거를 찾은 상태이므로 그것을 받아 적는다 — LLM 호출은 늘지
    않는다. 청크 앞부분을 그대로 싣던 종전 동작에서는 근거가 청크 뒤쪽에 있을 때 인용에
    아예 나타나지 않아, 화면이 "출처는 이 문서다"까지만 말할 수 있었다.
    """
    content_ko = "GET 요청에도 멱등키가 적용되나요? [[fake:sentences=1,supported=1]]"
    tail = "Idempotency keys are ignored on GET and DELETE requests."
    body = (
        "Write endpoints accept an optional Idempotency-Key header. "
        + "The key is stored with the request fingerprint for 24 hours. " * 8
        + tail
    )
    # 근거가 청크 앞부분 **밖**에 있어야 이 테스트가 구분력을 갖는다.
    assert body.index(tail) > prompts.QUOTE_MAX_LENGTH

    await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[(body, embedding_with_cosine(content_ko, HIGH_SIMILARITY))],
    )
    await db_session.commit()

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    quotes = [citation["quote"] for citation in detail["answer"]["citations"]]

    assert quotes == [tail]
    # 프론트 하이라이트가 원문에서 이 문자열을 그대로 찾는다 (`05 §6`).
    assert body.count(tail) == 1
