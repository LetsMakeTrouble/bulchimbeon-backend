"""`scripts/seed.py` 의 규약 (`08 §5`, `prompts/09-seed-deploy.md` 2번).

> ### 왜 스크립트에 테스트를 붙이는가
> 시드의 실패 모드는 **조용하다.** 청크가 덜 들어가도, 승인이 라이브 시연 질문을 삼켜도
> 스크립트는 초록으로 끝난다. 그리고 그 사실은 **발표 당일 화면에서** 드러난다.
> 여기서 보는 것은 셋이다:
>
> 1. `seed/*.md` 4개가 업로드·인제스트를 거쳐 정확히 23청크가 되는가 (`08 §2`)
> 2. 승인 주입이 `NO_APPROVAL_KEYS` 계열을 **계열 통째로** 건너뛰는가 (`09 §2`)
> 3. 그 제외가 뚫렸을 때 `assert_live_questions_not_reusable` 이 **실제로 잡는가**
>
> ⚠️ `--with-history` 의 등급 분포는 여기서 볼 수 없다. FakeLLM 의 해시 임베딩으로는 전부
> 강제 🔴 이 된다 (`06 §5`) — 그 확인은 실 LLM 실행과 `scripts/eval_questions.py` 몫이다.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Chunk, Document, DocumentVersion
from app.models.project import Project
from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_VERIFIED,
    GRADE_GREEN,
    GRADE_RED,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_HELD,
    Answer,
    Question,
)
from app.models.review_card import CARD_REASON_GREEN, CARD_REASON_RED, Feedback
from app.models.user import User
from app.services import review_card_service
from scripts import seed
from scripts.demo_questions import (
    BY_KEY,
    GREEN_EXPECTED_FAMILIES,
    HISTORY,
    LIVE_ONLY_KEYS,
    NO_APPROVAL_KEYS,
)
from tests.pipeline_helpers import as_uuid, embedding_with_cosine, seed_official_qa
from tests.review_helpers import Team, build_team


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "seed.test")


@pytest_asyncio.fixture
async def project(db_session: AsyncSession, team: Team) -> Project:
    loaded = await db_session.get(Project, as_uuid(team.project_id))
    assert loaded is not None
    return loaded


# --- 질문셋 구성 (`08 §5` 3번, `09 §2`) ----------------------------------------------------


def test_history_never_contains_live_only_questions_verbatim() -> None:
    """Q8·Q10 **원문**은 이력에 없다 — 라이브 재현용이다 (`09 §2`).

    변형은 있어도 된다. 🔴 `no_evidence` 는 공식 Q&A 를 만들지 않으므로 재사용 경로를
    오염시키지 않는다.
    """
    verbatim = {item.content_ko for item in HISTORY if item.verbatim}
    for key in LIVE_ONLY_KEYS:
        assert BY_KEY[key].content_ko not in verbatim, f"{key} 원문이 이력에 들어갔다"

    # Q8 계열 변형은 오히려 있어야 한다 — 등급 분포를 만드는 재료다.
    assert any(item.family == "Q8" for item in HISTORY)


def test_no_approval_families_are_excluded_wholesale() -> None:
    """제외는 **계열 통째로**다 (`09 §2`).

    원문만 막고 변형을 승인하면 그 변형이 공식 Q&A 가 되고, 데모 당일 원문이 그 Q&A 에 걸려
    재사용된다 — §4 1단계의 "🟢 87% + 인용"이 화면에서 사라진다.
    """
    for item in HISTORY:
        assert item.approvable == (item.family not in NO_APPROVAL_KEYS)

    assert not any(item.approvable for item in HISTORY if item.family in NO_APPROVAL_KEYS)


def test_history_size_and_green_weighting() -> None:
    """100~120건 · 🟢 기대가 35건 이상 (`08 §5` 3번).

    🟢 기대가 모자라면 실제 발행 🟢 이 D25 의 30건에 못 미쳐 `grade_accuracy` 가 전 등급
    "표본 부족"이 된다 — 발표 마지막 화면이 비는 실패 모드다.

    ⚠️ **상한이 60 에서 130 으로 올라갔다** (배포 세션 실측, 2026-08-09).
    "🟢 기대 35건"은 "🟢 **발행** 30건"을 보장하지 않는다 — 배포 DB 실측에서 58건 중
    🟢 발행은 **16건(27.6%)** 뿐이었다. 패러프레이즈는 원문과 다른 문장이라 매칭률이
    그대로 재현되지 않는다. D25 의 30건은 발행된 표본 수이므로 총량으로 흡수하는 수밖에 없다.
    상한을 다시 낮추려면 `eval_questions.py` 로 **발행률을 먼저 재측정**하라.
    """
    # ⚠️ **하한도 지킨다.** 실측 발행률 27.6% 기준 30건을 채우려면 ~110건이 필요하므로,
    #    45 로 줄이면 🟢 발행이 ~12건이 되어 25분짜리 실 LLM 실행 **뒤에** 종료 코드 1 이 난다.
    assert 100 <= len(HISTORY) <= 130
    green_expected = sum(1 for item in HISTORY if item.family in GREEN_EXPECTED_FAMILIES)
    assert green_expected >= 35
    # 🔴 계열은 등급 분포 확인용으로만 (`09 §2` — "8~10건에 그친다").
    assert 8 <= len(HISTORY) - green_expected <= 10
    # 같은 질문을 두 번 던지지 않는다 — 두 번째는 재사용 경로로 빠져 표본이 아니게 된다.
    assert len({item.content_ko for item in HISTORY}) == len(HISTORY)


# --- `--reset` 커버리지 (`08 §5` 4번, D19) --------------------------------------------------


def test_reset_covers_every_project_scoped_table() -> None:
    """`--reset` 이 **프로젝트 스코프 테이블 전부**를 지우는가 (D19 — 삭제 API 가 없다).

    ⚠️ 이 테스트가 지키는 것은 순서가 아니라 **커버리지**다. 나중에 테이블이 하나 늘었을 때
    빠뜨리면 `--reset` 이 FK 위반으로 터지는데, 시드를 돌리는 시점은 보통 **발표 전날 밤**이다.

    제외는 두 개뿐이고 **둘 다 이유가 다르다**:
    - `users` — 데모 유저는 지우지 않고 재사용한다 (`seed.py` 모듈 독스트링).
    - `job_runs` — 애초에 **프로젝트 스코프가 아니다**(`project_id` 컬럼이 없다).
      프로세스가 무엇을 했는지의 기록이라 특정 프로젝트를 지운다고 사라질 것이 아니다.

    ⛔ 새 테이블을 여기 제외 목록에 넣으려면 **`project_id` 가 정말 없는지** 확인하라.
    있는데 넣으면 지운 프로젝트의 흔적이 남아 다음 시드의 집계에 섞인다.
    """
    from uuid import uuid4

    from app.database import Base

    # ⚠️ "project_id 컬럼이 있나"로 자동 판별할 수 없다 — 다수 테이블이 `question_id`·
    #    `document_version_id` 를 거쳐 **간접적으로** 프로젝트에 묶인다. 목록을 손으로 적고,
    #    새 테이블이 생기면 이 어서션이 걸려 판단을 강제한다.
    exempt = {"users", "job_runs"}

    covered = {statement.table.name for statement in seed._reset_statements([uuid4()])}
    assert set(Base.metadata.tables) - covered == exempt


# --- 업로드·인제스트 (`08 §5` 2번) ---------------------------------------------------------


async def test_seed_documents_ingest_to_23_chunks(
    db_session: AsyncSession, team: Team, project: Project
) -> None:
    """`seed/*.md` 4개가 **운영과 같은 업로드 경로**로 들어가 23청크가 된다.

    `tests/demo_corpus.py` 는 청커만 부르지만 여기는 `create_document` → `run_ingest` 를
    그대로 태운다 — 파싱·청킹·임베딩·활성 전환까지 붙어야 검색 대상이 되기 때문이다.
    """
    uploader = await db_session.get(User, as_uuid(team.owner.id))
    assert uploader is not None

    await seed.upload_seed_documents(db_session, project, uploader)
    assert await seed.assert_chunk_count(db_session, project) == seed.EXPECTED_CHUNK_COUNT

    documents = list(
        (await db_session.scalars(select(Document).where(Document.project_id == project.id))).all()
    )
    assert len(documents) == len(seed.SEED_DOCUMENTS)

    # 검색 대상 3중 조건 (`06 §2` ③) — 활성 문서 · 활성 버전 · ready. 하나라도 빠지면
    # 청크가 DB 에 있어도 근거로 쓰이지 않는다.
    searchable = await db_session.scalar(
        select(func.count())
        .select_from(Chunk)
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.project_id == project.id,
            DocumentVersion.is_active.is_(True),
            DocumentVersion.ingest_status == "ready",
        )
    )
    assert searchable == seed.EXPECTED_CHUNK_COUNT


# --- 라이브 시연 경로 보호 어서션 (`09 §2`) -------------------------------------------------


async def test_live_guard_passes_when_nothing_was_approved(
    db_session: AsyncSession, project: Project
) -> None:
    """공식 Q&A 가 없으면 어서션은 통과한다 — 제외 목록이 지켜진 정상 상태다."""
    await seed.assert_live_questions_not_reusable(db_session, project)


async def test_live_guard_catches_a_leaked_official_qa(
    db_session: AsyncSession, team: Team, project: Project
) -> None:
    """⭐ 제외 목록이 뚫렸을 때 어서션이 **실제로 잡는가**.

    Q8 원문과 가까운 공식 Q&A 를 일부러 심는다 — 승인 주입이 Q8 계열 변형을 확정했을 때
    생기는 상태다. 이걸 못 잡으면 데모 당일 Q8 이 재사용 경로로 빠져 시나리오 B 가 사라진다.
    """
    # 어서션은 ① 번역문(`[en] {원문}`)을 임베딩해 조회한다 — 같은 축에 심어야 재현된다.
    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(BY_KEY["Q8"].content_ko, 0.95),
        question_ko=BY_KEY["Q8"].content_ko,
        question_en=f"[en] {BY_KEY['Q8'].content_ko}",
        answer_ko="일본은 20일입니다.",
        answer_en="Japan is 20 days.",
    )
    await db_session.commit()

    with pytest.raises(SystemExit, match="Q8"):
        await seed.assert_live_questions_not_reusable(db_session, project)


# --- 승인 주입 (`08 §5` 3번) ---------------------------------------------------------------


def _paraphrase_of(family: str) -> str:
    """이 계열의 **패러프레이즈** 하나.

    표준 질문 원문은 `seed.CANONICAL_TEXTS` 로 승인·🔴확정 대상에서 빠진다 — 공식 Q&A 가
    되면 그 질문이 ② 재사용으로 빠져 `eval_questions.py` 가 파이프라인을 검증하지 못하기
    때문이다. 그래서 승인 경로를 테스트하려면 **실제로 승인되는 쪽**, 즉 이력에 들어가는
    변형 문장을 심어야 한다.
    """
    for item in HISTORY:
        if item.family == family and item.content_ko not in seed.CANONICAL_TEXTS:
            return item.content_ko
    # Q10 처럼 이력에 아예 없는 계열(라이브 전용)은 원문으로 돌아간다. 그런 계열은 어차피
    # 제외 목록에 걸려 건너뛰는 쪽이므로 이 fallback 이 테스트를 무르게 만들지 않는다.
    return BY_KEY[family].content_ko


async def _plant_answered(
    db: AsyncSession, team: Team, project: Project, family: str
) -> seed.AskedQuestion:
    """등급 산출을 우회해 "발행된 🟢 답변 + 카드"를 직접 심는다.

    여기서 보는 것은 **어떤 카드를 확정하는가**이지 등급이 어떻게 나오는가가 아니다 —
    그건 `test_demo_scenarios.py` 몫이다.
    """
    content_ko = _paraphrase_of(family)
    question = Question(
        project_id=project.id,
        asker_id=as_uuid(team.asker.id),
        content_ko=content_ko,
        content_en=f"[en] {content_ko}",
        status=QUESTION_STATUS_ANSWERED,
    )
    db.add(question)
    await db.flush()

    answer = Answer(
        question_id=question.id,
        grade=GRADE_GREEN,
        state=ANSWER_STATE_DRAFT,
        content_ko="근거 있는 답변입니다.",
        content_en="A grounded answer.",
    )
    db.add(answer)
    await db.flush()

    await review_card_service.create_card(
        db, question=question, answer=answer, reason=CARD_REASON_GREEN
    )
    return seed.AskedQuestion(
        family=family,
        content_ko=question.content_ko,
        question_id=question.id,
        grade=GRADE_GREEN,
        status=QUESTION_STATUS_ANSWERED,
        answer_id=answer.id,
        answer_state=ANSWER_STATE_DRAFT,
    )


async def _plant_red(
    db: AsyncSession, team: Team, project: Project, family: str
) -> seed.AskedQuestion:
    """🔴 보류 상태 — 답변은 초안으로 남고 카드는 `red`, 선택지가 붙어 있다.

    `answer-option` 은 `red` 카드에만 허용되므로(`review_card_service.ALLOWED_ACTIONS`)
    reason 을 `CARD_REASON_RED` 로 만든다.

    질문 문안은 `_paraphrase_of` 를 쓴다 — 🔴 확정도 공식 Q&A 를 만들므로 표준 원문은
    `seed.CANONICAL_TEXTS` 로 대상에서 빠진다.
    """
    content_ko = _paraphrase_of(family)
    question = Question(
        project_id=project.id,
        asker_id=as_uuid(team.asker.id),
        content_ko=content_ko,
        content_en=f"[en] {content_ko}",
        status=QUESTION_STATUS_HELD,
    )
    db.add(question)
    await db.flush()

    answer = Answer(
        question_id=question.id,
        grade=GRADE_RED,
        state=ANSWER_STATE_DRAFT,
        # ⚠️ 강제 🔴 초안은 `None` 이 아니라 **`""`** 다 (`answer.py` — `" ".join([])`).
        #    `None` 은 "답변 행 자체가 없다"(D23 `failed`)는 뜻이고, `lesson_service` 가
        #    그 둘을 갈라 처리하므로 `None` 으로 심으면 **운영이 타는 경로를 안 태운다.**
        content_ko="",
        content_en="",
        question_struct={
            "background": "Background.",
            "question": "Which applies?",
            "options": ["Option A applies.", "Option B applies."],
        },
    )
    db.add(answer)
    await db.flush()

    await review_card_service.create_card(
        db, question=question, answer=answer, reason=CARD_REASON_RED
    )
    return seed.AskedQuestion(
        family=family,
        content_ko=question.content_ko,
        question_id=question.id,
        grade=GRADE_RED,
        status=QUESTION_STATUS_HELD,
        answer_id=answer.id,
        answer_state=ANSWER_STATE_DRAFT,
    )


async def test_resolve_red_cards_fills_the_red_accuracy_numerator(
    db_session: AsyncSession, team: Team, project: Project
) -> None:
    """🔴 카드 확정이 `grade_accuracy` 🔴 의 분자를 만든다 (D25).

    ⚠️ 이게 없으면 지표 화면에 **"🔴 정확도 0%"** 가 뜬다. 🔴 답변은 질문자에게 발행되지
    않아 `correct` 피드백 경로가 없으므로(`accuracy_service.for_grade`), 담당자 확정만이
    분자를 만든다. "AI 의 🔴 판정이 전부 틀렸다"로 읽히는 화면이 나오는 실패 모드다.
    """
    answerer = await db_session.get(User, as_uuid(team.owner.id))
    assert answerer is not None

    # 제외 계열 하나 + 확정 대상 둘. RED_RESOLVE_EVERY=2·TAKE=1 이라 대상 중 첫 건만 확정된다.
    excluded_family = sorted(seed.NO_RED_RESOLVE_KEYS & set(BY_KEY))[0]
    target_family = sorted(set(BY_KEY) - seed.NO_RED_RESOLVE_KEYS - LIVE_ONLY_KEYS)[0]

    excluded = await _plant_red(db_session, team, project, excluded_family)
    targets = [await _plant_red(db_session, team, project, target_family) for _ in range(2)]
    await db_session.commit()

    resolved = await seed.resolve_red_cards(db_session, project, [excluded, *targets], answerer)
    assert resolved == 1, "제외 계열은 건너뛰고 대상 계열의 절반만 확정해야 한다"

    states = dict(
        (
            await db_session.execute(
                select(Answer.id, Answer.state).where(
                    Answer.id.in_([excluded.answer_id, *[t.answer_id for t in targets]])
                )
            )
        ).all()
    )
    assert states[excluded.answer_id] == ANSWER_STATE_DRAFT, (
        f"{excluded_family} 계열은 확정되지 않는다 — 라이브 확인 경로가 재사용으로 빠진다"
    )
    assert states[targets[0].answer_id] == ANSWER_STATE_VERIFIED


async def test_inject_feedback_skips_red_and_varies_voters(
    db_session: AsyncSession, team: Team, project: Project
) -> None:
    """ "맞았다" 주입이 (a) 🔴 을 건너뛰고 (b) 두 번째 투표자를 **일부에만** 붙인다.

    ⚠️ 승인과 달리 계열 제한이 없다 — correct 피드백은 공식 Q&A 를 만들지 않으므로 데모 당일
    재사용 경로를 오염시키지 않는다. 대신 2건이 모이면 카드가 `recommend_approve` 로 올라오는데
    (룰 3), **전부 2건이면 큐 전체가 승인 추천으로 떠서** 브리핑 화면이 의미를 잃는다.
    """
    first = await db_session.get(User, as_uuid(team.asker.id))
    second = await db_session.get(User, as_uuid(team.asker2.id))
    assert first is not None and second is not None
    askers = [first, second]

    asked = [await _plant_answered(db_session, team, project, "Q2") for _ in range(3)]
    # 🔴 은 발행되지 않았으므로 피드백 대상이 아니다.
    red = await _plant_answered(db_session, team, project, "Q7")
    red.grade = GRADE_RED
    asked.append(red)
    await db_session.commit()

    injected = await seed.inject_feedback(db_session, asked, askers)

    verdicts = (
        await db_session.execute(
            select(Feedback.answer_id, func.count()).group_by(Feedback.answer_id)
        )
    ).all()
    counts = dict(verdicts)

    assert red.answer_id not in counts, "🔴 답변에는 피드백을 붙이지 않는다"
    assert injected == sum(counts.values())
    # 첫 건만 2인 투표(`index % 3 == 0`), 나머지는 1인.
    assert sorted(counts.values()) == [1, 1, 2]


async def test_inject_approvals_skips_excluded_families(
    db_session: AsyncSession, team: Team, project: Project
) -> None:
    """승인 주입이 `NO_APPROVAL_KEYS` 계열을 건너뛴다 (`09 §2`)."""
    answerer = await db_session.get(User, as_uuid(team.owner.id))
    assert answerer is not None

    # ⚠️ 계열 이름을 하드코딩하지 않는다 — `NO_APPROVAL_KEYS` 는 데모 1단계 질문이 바뀌면
    #    함께 바뀐다(2026-08-09 Q1 → Q2). 상수에서 뽑아야 목록이 바뀌어도 **이 테스트가
    #    검증하려는 것**(제외 계열은 건너뛴다)이 그대로 유지된다.
    excluded = sorted(NO_APPROVAL_KEYS & set(BY_KEY))[0]
    approvable = sorted(set(BY_KEY) - NO_APPROVAL_KEYS - LIVE_ONLY_KEYS)[0]

    asked = [
        await _plant_answered(db_session, team, project, excluded),
        await _plant_answered(db_session, team, project, approvable),
    ]
    await db_session.commit()

    approved = await seed.inject_approvals(db_session, project, asked, answerer)
    assert approved == 1, f"제외 계열({excluded})은 건너뛰고 {approvable} 만 승인해야 한다"

    # ⚠️ 컬럼 select 로 읽는다. `db.get()` 은 identity map 에 남은 객체를 돌려주고, 그 위에서
    #    `expire_all()` 뒤 속성에 접근하면 async 에서 `MissingGreenlet` 이 된다.
    states = dict(
        (
            await db_session.execute(
                select(Answer.id, Answer.state).where(
                    Answer.id.in_([item.answer_id for item in asked])
                )
            )
        ).all()
    )
    assert states[asked[0].answer_id] == ANSWER_STATE_DRAFT, f"{excluded} 계열은 확정되지 않는다"
    assert states[asked[1].answer_id] == ANSWER_STATE_VERIFIED

    # 승인은 공식 Q&A 를 만든다 (`06 §3`) — 그래서 제외 목록이 필요한 것이다.
    # 제외 계열이 섞여 들어갔다면 여기서 어서션이 잡는다.
    await seed.assert_live_questions_not_reusable(db_session, project)
