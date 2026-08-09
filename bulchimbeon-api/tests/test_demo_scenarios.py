"""M9 DoD — 검증 질문셋 Q1~Q13 기대 등급 재현 (`08 §3`, `prompts/09-seed-deploy.md` 3번).

> ### 이 파일이 검증하는 것과 하지 못하는 것
> **하는 것**: 실제 `seed/*.md` 22청크를 검색 대상으로 두고, 기대 근거 청크가 `retrieval_top_k`
> (6) 안에 **top-1 으로** 들어오는지 · 리스케일과 `min(S, G)` 가 기대 등급을 만드는지 ·
> 강제 🔴 3종(conflict / no_evidence / similarity_floor)이 분리되는지 · Q10 재사용 경로.
>
> **못 하는 것**: 임베딩 품질. `FakeLLMProvider` 의 임베딩은 해시 기반이라 의미 유사도를
> 반영하지 않는다 (`06 §5`). 그래서 목표 코사인을 **직접 심는다** — "실제 질문이 실제 문서와
> 얼마나 가까운가"는 원리적으로 여기서 나오지 않으며, 그 몫은 M-1 게이트와
> `scripts/eval_questions.py`(실 LLM) 다.

> ### ⚠️ 🔴 케이스는 **시각에 의존하지 않게** 고정한다 (`09 §3`)
> `low_confidence` 는 담당자 DND 시간대에 🟡 로 강등된다 (룰 6 · D2). 강등 대상이 아닌
> **강제 🔴 4종**만 쓰면 실행 시각과 무관하게 결정적이다. 특히 Q9 는 `08 §3` 이
> `no_evidence` 와 `low_confidence` 를 모두 정답으로 두었으므로, 여기서는 `not_answerable`
> 로 고정해 🔴 을 못박는다 — 고정하지 않으면 Q9 만 실행 시각에 따라 흔들린다.
>
> 여기에 더해 `build_team` 이 프로젝트의 DND 창을 아예 닫아 둔다
> (`helpers.close_dnd_window`). 즉 방어가 두 겹이다 — 강제 🔴 은 창이 열려 있어도 🔴 을
> 유지해야 하고(D2), 그 규칙 자체의 검증은 `test_pipeline.py` 의 DND 테스트들이 맡는다.
"""

from collections import Counter
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.question import (
    ANSWER_SOURCE_GENERATED,
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_VERIFIED,
    GRADE_GREEN,
    GRADE_RED,
    HELD_REASON_CONFLICT,
    HELD_REASON_NO_EVIDENCE,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_HELD,
)
from scripts.demo_questions import BY_KEY
from tests.demo_corpus import EXPECTED_CHUNK_COUNT, seed_corpus
from tests.demo_corpus import plant_seed_corpus as plant
from tests.pipeline_helpers import ask, embedding_with_cosine, seed_official_qa
from tests.review_helpers import Team, build_team, question_detail

# --- 심는 코사인 ---------------------------------------------------------------------------
# S = round(clamp((sim_raw - s_floor) / (s_ceil - s_floor), 0, 1) × 100)
# — `s_floor` 0.25 · `s_ceil` 0.679 (M-1 게이트 확정값).
SIM_GREEN = 0.62  # → S = 86 (🟢 하한 80 위)
EXPECTED_GREEN_S = 86
SIM_MID = 0.50  # → S = 58 (`similarity_floor` 위, 🟢 아래)

# ⚠️ **`DEFAULT_SETTINGS` 에서 파생시킨다 — 숫자를 적지 마라.**
#    이 상수의 목적은 "`similarity_floor` **바로 아래**"라는 경계를 고정하는 것이다.
#    리터럴로 두면 임계값이 움직였을 때(2026-08-09 에 0.423 → 0.444 로 실제로 움직였다)
#    여유가 벌어져 **경계 테스트가 경계를 더 이상 못 잡는데도 초록으로 통과한다.**
_FLOOR = float(DEFAULT_SETTINGS["similarity_floor"])
SIM_BELOW_FLOOR = round(_FLOOR - 0.003, 4)
assert SIM_BELOW_FLOOR < _FLOOR < SIM_MID, "경계 상수가 임계값을 사이에 두지 않는다"


# `06 §5` FakeLLM 마커. 조합의 정본은 `fake_provider.py` 독스트링이다.
MARKER_GREEN = "[[fake:sentences=3,supported=3]]"
MARKER_CONFLICT = "[[fake:conflict]]"
MARKER_NOT_ANSWERABLE = "[[fake:not_answerable]]"

API_SPEC = "Orders API Specification v2.1"
REFUND = "Refund Policy v1"
GUIDE = "Integration Guide"
NOTES = "Partner Sync Notes — July 2026"


@dataclass(frozen=True)
class Case:
    """질문 1건의 재현 조건. `boosts` 는 `heading_path` → 심을 원시 코사인이다."""

    key: str
    marker: str
    boosts: dict[tuple[str, ...], float]
    expected_grade: str
    held_reason: str | None = None
    # 인용이 가리켜야 할 청크. 🔴 은 발행 문장이 없어 인용도 없다.
    expected_citation: tuple[str, ...] | None = None
    note: str = field(default="")

    @property
    def content_ko(self) -> str:
        """마커를 붙인 실제 질문 본문.

        ① 이 `content_en = "[en] {원문}"` 을 만들므로 마커가 ②④⑦ 프롬프트까지 실려 간다.
        """
        return f"{BY_KEY[self.key].content_ko} {self.marker}"


def _green(key: str, heading: tuple[str, ...]) -> Case:
    return Case(
        key=key,
        marker=MARKER_GREEN,
        boosts={heading: SIM_GREEN},
        expected_grade=GRADE_GREEN,
        expected_citation=heading,
    )


CASES: tuple[Case, ...] = (
    _green("Q1", (API_SPEC, "GET /v2/orders/{order_id}")),
    _green("Q2", (API_SPEC, "Authentication")),
    _green("Q3", (API_SPEC, "Currencies")),
    _green("Q4", (API_SPEC, "Pagination")),
    _green("Q5", (NOTES, "API and Rate Limits")),
    _green("Q6", (REFUND, "Shipping Fees")),
    Case(
        key="Q7",
        marker=MARKER_CONFLICT,
        # 두 근거를 함께 top-k 에 올려 `08 §2` 의 의도된 장치를 재현한다.
        # ⚠️ 다만 **FakeLLM 에서 이 두 번째 boost 는 판정에 영향을 주지 않는다** —
        #    `fake_provider._sentences_payload` 가 마커만 보고 `conflict=true` 를 내기 때문이다.
        #    "60/min 과 100/min 이 함께 검색되면 모델이 모순을 알아보는가"는 실 LLM 이라야
        #    나오며 `scripts/eval_questions.py` 가 본다. 여기 남겨 둔 이유는 검색 문맥을
        #    실제와 같게 두기 위해서다.
        boosts={
            (API_SPEC, "Rate Limiting"): SIM_GREEN,
            (NOTES, "API and Rate Limits"): 0.60,
        },
        expected_grade=GRADE_RED,
        held_reason=HELD_REASON_CONFLICT,
        note="60/min(api-spec) vs 100/min(meeting-notes) — 강제 🔴, DND 에서도 유지된다.",
    ),
    Case(
        key="Q8",
        marker=MARKER_NOT_ANSWERABLE,
        # `Regions` 는 `ap-northeast` 가 있다는 인프라 사실만 말한다 — 검색은 되지만 근거가
        # 아니므로 ④ 가 `not_answerable` 을 낸다 (`06 §2` ④, `08 §2` 의도된 장치).
        boosts={(API_SPEC, "Regions"): SIM_MID},
        expected_grade=GRADE_RED,
        held_reason=HELD_REASON_NO_EVIDENCE,
        note="주제만 언급하는 청크는 근거가 아니다 — 검색이 되는데도 🔴 이 나오는 경로.",
    ),
    Case(
        key="Q9",
        marker=MARKER_NOT_ANSWERABLE,
        # 미끼: "Payment gateway integration is on track." — 어휘만 겹치고 PG 사 이름이 없다.
        boosts={(NOTES, "JP Launch"): SIM_MID},
        expected_grade=GRADE_RED,
        held_reason=HELD_REASON_NO_EVIDENCE,
        note="일반 상식 폴백 금지(룰 6)의 미끼 케이스. `09 §3` 대로 🔴 을 고정했다.",
    ),
    _green("Q11", (GUIDE, "Webhook Signature Verification")),
    _green("Q12", (API_SPEC, "Idempotency")),
    _green("Q13", (API_SPEC, "Sandbox")),
)


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "demo-scenarios.test")


def test_seed_corpus_is_22_chunks() -> None:
    """`08 §2` — `##` 섹션 하나가 청크 하나 (10 + 5 + 5 + 2).

    청크 수가 `retrieval_top_k`(6)보다 적으면 검색이 "전부 반환"으로 퇴화하는데도 파이프라인은
    초록으로 돈다 — 조용히 깨지는 실패 모드라 여기서 못박는다 (`08 §5`).
    ⚠️ 제목 줄만 있는 섹션은 청크가 아니다 (`utils/chunking._has_prose`).
    """
    corpus = seed_corpus()
    assert len(corpus) == EXPECTED_CHUNK_COUNT

    assert Counter(chunk.doc_filename for chunk in corpus) == {
        "api-spec.md": 10,
        "refund-policy.md": 5,
        "integration-guide.md": 5,
        "meeting-notes-2026-07.md": 2,
    }
    # 모든 청크가 `H1 > H2` 두 단계 경로를 갖는다 — 빵부스러기가 화면에 그대로 찍힌다 (`05 §6`).
    assert all(len(chunk.heading_path) == 2 for chunk in corpus)


@pytest.mark.parametrize("case", CASES, ids=[case.key for case in CASES])
async def test_expected_grade_is_reproduced(
    client: AsyncClient, db_session: AsyncSession, team: Team, case: Case
) -> None:
    """`08 §3` 기대 등급 표 재현."""
    await plant(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        question_ko=case.content_ko,
        boosts=case.boosts,
    )

    accepted = await ask(client, team.asker, team.project_id, case.content_ko)
    view = await question_detail(client, team.asker, accepted["question_id"])

    if case.expected_grade == GRADE_RED:
        # 🔴 은 질문자에게 발행하지 않는다 — 초안은 카드에서만 보인다 (`04 §2`).
        assert view["status"] == QUESTION_STATUS_HELD, case.note
        assert view["answer"] is None
        assert view["held_info"]["reason"] == case.held_reason, case.note
        return

    assert view["status"] == QUESTION_STATUS_ANSWERED
    answer = view["answer"]
    assert answer["grade"] in BY_KEY[case.key].expected_grades
    assert answer["grade"] == case.expected_grade
    assert answer["source"] == ANSWER_SOURCE_GENERATED
    # S 가 리스케일식대로 나오는지 본다 (`s_floor`/`s_ceil` 회귀 감지).
    # ⚠️ 여기 세 줄은 **`min(S, G)` 를 판별하지 못한다** — 모든 케이스가 G=100 이라
    #    `matching_rate = S` 로 구현해도 통과한다. `min` 의 진짜 검증은 G<100 케이스를 가진
    #    `test_pipeline.py::test_pruning_does_not_raise_the_matching_rate` 에 있다.
    assert answer["search_score"] == EXPECTED_GREEN_S
    assert answer["grounding_score"] == 100
    assert answer["matching_rate"] == EXPECTED_GREEN_S

    # boost 한 청크가 실제 SQL 랭킹에서 top-1 으로 올라왔는가.
    # ⚠️ 이 단언이 잡는 것은 **`1 - (embedding <=> :q)` 의 부호**다 — 거리를 유사도로 그대로
    #    쓰면 boost 청크가 꼴찌가 되어 즉시 깨진다 (`04 §7` 의 지뢰).
    #    "확장된 시드 섹션이 실제 임베딩에서 top-6 에 드는가"(Q11~Q13 의 원래 취지)는 여기서
    #    나오지 않는다 — 목표 코사인을 직접 심었으므로 구조상 보장된다. 그 확인은 실 LLM 을
    #    쓰는 `scripts/eval_questions.py` 몫이다.
    citations = answer["citations"]
    assert citations, "🟢 답변에는 인용이 붙는다"
    assert tuple(citations[0]["heading_path"]) == case.expected_citation
    assert citations[0]["doc_title"] == case.expected_citation[0]


async def test_q8_below_similarity_floor_is_forced_red(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """Q8 의 두 번째 방어선 — top-1 원시 코사인이 `similarity_floor` 미만이면 강제 🔴.

    `SIM_BELOW_FLOOR` 는 `DEFAULT_SETTINGS["similarity_floor"]` 에서 파생돼 **항상 바로 아래**다.
    ④ 가 무엇을 내든 이 관문에서 먼저 걸린다 — 벡터 검색은 항상 "가장 가까운 무언가"를
    돌려주므로 하한이 없으면 무관한 청크로 답을 만들게 된다 (`06 §2` ③).
    """
    # 마커 없음 — ④ 까지 갔다면 🟢 이 나왔을 조건에서 검색 하한만으로 🔴 이 되는지 본다.
    content_ko = f"{BY_KEY['Q8'].content_ko} {MARKER_GREEN}"
    await plant(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        question_ko=content_ko,
        boosts={(API_SPEC, "Regions"): SIM_BELOW_FLOOR},
    )

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    view = await question_detail(client, team.asker, accepted["question_id"])

    assert view["status"] == QUESTION_STATUS_HELD
    assert view["held_info"]["reason"] == HELD_REASON_NO_EVIDENCE


async def test_q10_reuses_the_official_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """Q10 — Q8 확정 후 공식 Q&A 재사용 (`08 §3`, `08 §4` 5단계).

    ⚠️ 재사용은 임계값 하나로 결정되지 않는다. 원시 코사인이 `reuse_threshold` 이상이면
    **후보**가 되고, ② 2차 게이트(LLM yes/no)가 `yes` 일 때만 확정 원문이 나간다 (`06 §2` ②).
    FakeLLM 의 기본 게이트 응답이 `yes` 이므로 여기서는 통과 경로를 본다.
    """
    content_ko = BY_KEY["Q10"].content_ko
    confirmed_ko = "일본은 현지법에 따라 20일입니다."

    # M-1 실측 Q8↔Q10 원시 코사인 0.9551 (`06 §2` ②) — `reuse_threshold`(0.925) 위다.
    official_qa = await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, 0.9551),
        question_ko=BY_KEY["Q8"].content_ko,
        question_en="Does the refund policy apply the same way in the Japan region?",
        answer_ko=confirmed_ko,
        answer_en="Japan is 20 days (local law).",
    )
    # 근거 청크도 함께 심는다 — 재사용은 ③ 검색 **이전**에 갈리므로 이 청크는 쓰이지 않아야 한다.
    await plant(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        question_ko=content_ko,
        boosts={(REFUND, "Standard Refund Window"): SIM_GREEN},
    )

    accepted = await ask(client, team.asker2, team.project_id, content_ko)
    view = await question_detail(client, team.asker2, accepted["question_id"])
    answer = view["answer"]

    assert view["status"] == QUESTION_STATUS_ANSWERED
    assert answer["grade"] == GRADE_GREEN
    assert answer["source"] == ANSWER_SOURCE_REUSED
    assert answer["state"] == ANSWER_STATE_VERIFIED, "재사용은 draft 를 거치지 않는다 (D11)"
    # 확정 당시 한국어 원문 **그대로**다. 재번역 금지 (룰 4·D5).
    assert answer["content_ko"] == confirmed_ko
    assert answer["matching_rate"] is None, "재사용은 원시 코사인 판정이라 % 를 표시하지 않는다"
    assert answer["citations"] == [], "검색을 건너뛰었으므로 인용이 없다"
    assert answer["official_qa"]["id"] == str(official_qa.id)
