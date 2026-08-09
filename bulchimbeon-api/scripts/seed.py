"""데모 시드 (`08 §5`, `prompts/09-seed-deploy.md` 2번).

```bash
set -a; . ./.env; set +a
uv run python scripts/seed.py --reset                  # 유저·프로젝트·문서 4개 + 인제스트
uv run python scripts/seed.py --reset --with-history   # + 질문 123건 이력·지표 (25~30분)
```

> ### 서비스 레이어를 직접 부른다 (HTTP 아님, `09 §2`)
> 권한 의존성이 끼지 않고, 무엇보다 **카드 상세(`GET /review-cards/{id}`)를 부르지 않는다** —
> 그 호출이 `first_viewed_at` 을 찍으므로 시드가 건드리면 `card_handle_30s_rate`(M7)가
> "열지도 않은 카드"를 열람한 것으로 세게 된다.

> ### `--with-history` 는 **실 LLM 을 전제한다**
> `FakeLLMProvider` 의 임베딩은 해시 기반이라 질문과 청크 사이 코사인이 사실상 0 이다
> (`06 §5` 한계). `similarity_floor`(0.444)에 전부 걸려 **123건이 모두 강제 🔴 `no_evidence`**
> 가 되므로 등급 분포도 `grade_accuracy` 도 만들어지지 않는다. 그래서 fake 로 돌릴 때는
> 경고를 찍고 진행한다 — 스크립트 자체를 무료로 점검할 때만 쓴다.
>
> 실 LLM 비용 실측(2026-08-09, `03 §4.2`): 이력 123건이 **370 호출**이고 질문당 평균
> 2.94회다(🟢🟡 3회 · 🔴 2~4회). 재질문 6건이 **+12**, 라이브 경로 보호 어서션이 **+5**
> (Q2·Q7·Q8·Q10 번역 4 + 임베딩 1) 붙는다. 그 5회는 파이프라인 밖이라 `quota.py` 집계에
> 잡히지 않는다. 시간은 25~30분, API 비용은 약 **$1.3** 이다.
> ⛔ **시드도 `daily_llm_call_limit`(500)의 적용을 받는다** — 파이프라인을 인프로세스로
> 부르므로 카운터가 이 프로세스에 쌓인다. **382/500 = 76% 소진**이고 여유는 118회 ≈
> **질문 39건**이다. 이력을 165건 근처로 늘리면 뒷부분이 조용히 강제 🔴 `quota_exceeded`
> 가 된다 — 늘릴 때는 한도도 함께 올려라.
> ⚠️ 시드는 API 서버와 **다른 프로세스**라 서버 쪽 카운터는 그대로 500 이 남는다.
> 발표 중 라이브 질문이 한도에 걸릴 걱정은 없다.
> **발표 당일 재실행은 피하고 전날 채워 둔다.**

> ### 멱등성 (`--reset`, D19)
> 프로젝트 삭제 API 가 MVP 에 없으므로 **DB 레벨에서** 지운다. 데모 유저는 지우지 않고
> 재사용한다 — `events.actor_id` 처럼 프로젝트 밖에서 유저를 참조하는 행이 남아 있으면
> 삭제가 FK 로 막히고, 어차피 시드가 같은 값으로 다시 덮어쓰기 때문이다.
"""

import argparse
import asyncio
import logging
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import UploadFile  # noqa: E402
from sqlalchemy import delete, func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.sql.expression import Executable  # noqa: E402

from app.config import DEFAULT_SETTINGS, settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.briefing_run import BriefingRun  # noqa: E402
from app.models.document import Chunk, Document, DocumentVersion  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.integration import Integration  # noqa: E402
from app.models.lesson import Lesson  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.official_qa import OfficialQA  # noqa: E402
from app.models.project import Guideline, Project, ProjectMember  # noqa: E402
from app.models.question import (  # noqa: E402
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_DRAFT,
    GRADE_GREEN,
    GRADE_RED,
    GRADE_YELLOW,
    QUESTION_STATUS_ANSWERED,
    Answer,
    AnswerCitation,
    Question,
)
from app.models.review_card import (  # noqa: E402
    CARD_STATUS_RESOLVED,
    FEEDBACK_CORRECT,
    Feedback,
    ReviewCard,
)
from app.models.user import User  # noqa: E402
from app.schemas.project import ProjectCreate  # noqa: E402
from app.schemas.question import FeedbackCreate, QuestionCreate  # noqa: E402
from app.services import (  # noqa: E402
    accuracy_service,
    document_service,
    feedback_service,
    project_service,
    question_service,
    review_card_service,
)
from app.services.llm import get_provider  # noqa: E402
from app.services.pipeline import answer as answer_pipeline  # noqa: E402
from app.services.pipeline import ingest, prompts, retrieval  # noqa: E402
from app.services.pipeline.llm_schemas import TranslationOut  # noqa: E402
from scripts.demo_questions import (  # noqa: E402
    BY_KEY,
    HISTORY,
    NO_APPROVAL_KEYS,
    NO_RED_RESOLVE_KEYS,
)

logger = logging.getLogger("seed")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = PROJECT_ROOT / "seed"

# --- 데모 설정 (`08 §1`) -----------------------------------------------------------------
DEMO_PASSWORD = "demo1234!"
PROJECT_NAME = "GlobalMart JP Launch"
PROJECT_DESCRIPTION = (
    "한국 커머스팀이 미국 개발 파트너(DevCorp)의 API로 일본 리전 런칭을 준비하는 프로젝트."
)
GUIDELINES = (
    "Answers must reference the exact API version. If a policy differs by region, always say "
    "which regions were checked. Prefer concise answers with field names in backticks."
)


@dataclass(frozen=True)
class DemoUser:
    email: str
    name: str
    language: str
    timezone: str


ANSWERER = DemoUser("mike@devcorp.example", "Mike Chen", "en", "America/New_York")
ASKERS = (
    DemoUser("jisoo@globalmart.example", "지수", "ko", "Asia/Seoul"),
    DemoUser("minjun@globalmart.example", "민준", "ko", "Asia/Seoul"),
)

# `08 §2` — 4개 문서. 제목은 문서 본문의 H1 을 그대로 쓴다.
SEED_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("api-spec.md", "Orders API Specification v2.1"),
    ("refund-policy.md", "Refund Policy v1"),
    ("integration-guide.md", "Integration Guide"),
    ("meeting-notes-2026-07.md", "Partner Sync Notes — July 2026"),
)

# `08 §2` — `##` 섹션 하나가 청크 하나다 (11 + 5 + 5 + 2).
#
# ⚠️ 어서션으로 두는 이유: 청크 수가 `retrieval_top_k`(6)보다 적으면 검색이 "전부 반환"으로
#    퇴화하고, 그래도 파이프라인은 초록으로 돈다 — **조용히 깨지는 실패 모드**다 (`08 §5`).
EXPECTED_CHUNK_COUNT = 23

# D25 — 등급별 표본이 이 값 미만이면 `grade_accuracy` 가 "표본 부족"으로 뜬다.
# 🟢 하나라도 숫자를 띄우려면 🟢 발행이 이만큼은 나와야 한다 (`08 §5`).
#
# ⚠️ **정본은 `accuracy_service.MIN_SAMPLE` 이다.** 여기 30 을 다시 적으면 시드의 게이트와
#    화면의 "표본 부족" 판정이 조용히 갈린다 (룰 3, `accuracy_service` 독스트링).
MIN_GREEN_SAMPLE = accuracy_service.MIN_SAMPLE

# 이력 답변 중 이 비율에 질문자 "맞았다"를, 저 비율에 담당자 승인을 주입한다.
# `grade_accuracy` 의 분자는 **`verified` 또는 correct 피드백 ≥1건**이므로(D25) 둘의 합집합이
# 실측 정확도가 된다. 데모에서 90% 근처가 나오도록 잡았다.
FEEDBACK_EVERY = 10  # 10건 중 7건에 "맞았다"
FEEDBACK_TAKE = 7
APPROVE_EVERY = 5  # 5건 중 2건에 담당자 승인
APPROVE_TAKE = 2
# 확정된 공식 Q&A 를 다시 묻는 질문 수. 이력에 ② 재사용 경로를 남기는 유일한 자리다
# (`run_reuse_followups` 독스트링 — 순서 때문에 본 이력에서는 재사용이 성립하지 않는다).
REUSE_FOLLOWUP_COUNT = 6

# 🔴 카드 확정 비율 — 담당자가 인박스를 절반쯤 처리한 상태를 만든다.
RED_RESOLVE_EVERY = 2
RED_RESOLVE_TAKE = 1


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------------------
# 1. 초기화
# --------------------------------------------------------------------------------------
def _reset_statements(project_ids: list[UUID]) -> list[Executable]:
    """`--reset` 이 지우는 것들. **의존 순서대로**다 (FK 에 `ON DELETE` 가 없다).

    ⚠️ `answers ↔ official_qas` 는 서로를 가리킨다 (`04 §2` — `use_alter` FK 두 개).
    그래서 official_qas 를 지우기 전에 answers 쪽 참조를 먼저 NULL 로 끊는다.
    """
    question_ids = select(Question.id).where(Question.project_id.in_(project_ids))
    answer_ids = select(Answer.id).where(Answer.question_id.in_(question_ids))
    document_ids = select(Document.id).where(Document.project_id.in_(project_ids))
    version_ids = select(DocumentVersion.id).where(DocumentVersion.document_id.in_(document_ids))

    return [
        update(Answer)
        .where(Answer.question_id.in_(question_ids))
        .values(official_qa_id=None, similar_official_qa_id=None),
        delete(Lesson).where(Lesson.project_id.in_(project_ids)),
        delete(AnswerCitation).where(AnswerCitation.answer_id.in_(answer_ids)),
        delete(Feedback).where(Feedback.answer_id.in_(answer_ids)),
        delete(ReviewCard).where(ReviewCard.project_id.in_(project_ids)),
        delete(OfficialQA).where(OfficialQA.project_id.in_(project_ids)),
        delete(Answer).where(Answer.question_id.in_(question_ids)),
        delete(Question).where(Question.project_id.in_(project_ids)),
        delete(Chunk).where(Chunk.document_version_id.in_(version_ids)),
        delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)),
        delete(Document).where(Document.project_id.in_(project_ids)),
        delete(Notification).where(Notification.project_id.in_(project_ids)),
        delete(BriefingRun).where(BriefingRun.project_id.in_(project_ids)),
        delete(Integration).where(Integration.project_id.in_(project_ids)),
        delete(Guideline).where(Guideline.project_id.in_(project_ids)),
        delete(Event).where(Event.project_id.in_(project_ids)),
        delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids)),
        delete(Project).where(Project.id.in_(project_ids)),
    ]


async def reset(db: AsyncSession) -> int:
    """기존 데모 프로젝트를 DB 레벨에서 지운다 (D19 — 삭제 API 가 없다).

    유저는 남긴다 — 모듈 독스트링 참조.
    """
    project_ids = list(
        (await db.scalars(select(Project.id).where(Project.name == PROJECT_NAME))).all()
    )
    if not project_ids:
        log("· --reset: 지울 데모 프로젝트가 없다.")
        return 0

    for statement in _reset_statements(project_ids):
        await db.execute(statement)
    await db.commit()

    # 업로드 원본도 함께 지운다. 남겨 두면 재실행마다 STORAGE_DIR 이 계속 자란다.
    for project_id in project_ids:
        target = settings.storage_dir / str(project_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    log(f"· --reset: 데모 프로젝트 {len(project_ids)}개와 그 하위 데이터를 지웠다.")
    return len(project_ids)


async def ensure_user(db: AsyncSession, spec: DemoUser) -> User:
    """이메일로 찾고 없으면 만든다. 있으면 데모 값으로 덮어쓴다 (재실행 멱등)."""
    user = await db.scalar(select(User).where(User.email == spec.email))
    if user is None:
        user = User(
            email=spec.email,
            password_hash=hash_password(DEMO_PASSWORD),
            name=spec.name,
            language=spec.language,
            timezone=spec.timezone,
        )
        db.add(user)
    else:
        user.password_hash = hash_password(DEMO_PASSWORD)
        user.name = spec.name
        user.language = spec.language
        user.timezone = spec.timezone
    await db.flush()
    return user


# --------------------------------------------------------------------------------------
# 2. 프로젝트 · 문서
# --------------------------------------------------------------------------------------
async def build_project(db: AsyncSession, answerer: User, askers: list[User]) -> Project:
    """담당자가 프로젝트를 만들고(D1) 질문자들이 초대 코드로 참여한다."""
    detail = await project_service.create_project(
        db, answerer, ProjectCreate(name=PROJECT_NAME, description=PROJECT_DESCRIPTION)
    )
    project = await project_service.load_project(db, detail.id)

    for asker in askers:
        await project_service.join_by_invite_code(db, asker, project.invite_code)

    await project_service.upsert_guideline(db, project.id, GUIDELINES, answerer.id)
    await db.commit()

    # `08 §1` 설정 표 — 기본값 그대로여야 한다. 넷은 데모 대사가 그 값에 의존하므로 확인한다.
    # `s_floor`·`s_ceil` 은 **둘 다** 봐야 한다 — 리스케일 분모가 `s_ceil - s_floor` 라서
    # 한쪽만 어긋나도 `08 §4` 1단계의 "🟢 87%" 가 달라진다.
    #
    # ⚠️ `assert` 가 아니라 `SystemExit` 다. `python -O` 로 돌리면 assert 는 통째로 사라지고,
    #    그러면 데모 재현을 지키는 관문이 조용히 없어진다.
    for key in ("retrieval_top_k", "similarity_floor", "s_floor", "s_ceil"):
        if project.settings[key] != DEFAULT_SETTINGS[key]:
            raise SystemExit(
                f"{key} 가 기본값과 다르다: {project.settings[key]} != {DEFAULT_SETTINGS[key]}"
            )

    log(f"· 프로젝트 '{project.name}' 생성 (invite_code={project.invite_code})")
    return project


async def upload_seed_documents(db: AsyncSession, project: Project, uploader: User) -> None:
    """`seed/*.md` 4개를 업로드 경로 그대로 태우고 인제스트가 끝날 때까지 기다린다.

    ⚠️ 새 진입점을 만들지 않는다 — `create_document` 가 확장자 화이트리스트·크기 상한·
    문서 수 상한·`safe_filename` 을 모두 통과시키는 **운영과 같은 경로**다 (M8 이 정리한
    바이트 진입점 `create_version_from_bytes` 가 그 안에 있다).
    """
    version_ids: list[UUID] = []

    for filename, title in SEED_DOCUMENTS:
        path = SEED_DIR / filename
        if not path.exists():
            raise SystemExit(f"시드 문서가 없다: {path}")

        payload = path.read_bytes()
        if b"\r\n" in payload:
            # `.gitattributes` 의 `*.md text eol=lf` 가 적용되지 않은 체크아웃이다.
            # CRLF 가 섞이면 `content_hash` 가 OS 마다 달라진다 (`03 §5.2`).
            raise SystemExit(f"CRLF 가 섞여 있다: {path} — `.gitattributes` 적용 후 재체크아웃.")

        upload = UploadFile(file=BytesIO(payload), filename=filename)
        _, version = await document_service.create_document(
            db,
            project_id=project.id,
            uploader=uploader,
            file=upload,
            title=title,
            auto_activate=True,
        )
        version_ids.append(version.id)

    await db.commit()

    # 인제스트는 자체 세션을 여는 백그라운드 진입점이다 (`03 §2` 원칙 4).
    # 시드는 스크립트이므로 태스크로 띄우지 않고 그대로 await 한다 — `ready` 대기가 곧 이것이다.
    for version_id in version_ids:
        await ingest.run_ingest(version_id, activate_on_ready=True)

    log(f"· 문서 {len(SEED_DOCUMENTS)}개 업로드·인제스트 완료")


async def assert_chunk_count(db: AsyncSession, project: Project) -> int:
    """`08 §5` 2번 — 청크 수 어서션. `retrieval_top_k` 보다 커야 검색이 no-op 가 아니다."""
    count = (
        await db.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.project_id == project.id)
        )
        or 0
    )
    top_k = int(project.settings["retrieval_top_k"])
    if count != EXPECTED_CHUNK_COUNT:
        raise SystemExit(
            f"청크 수가 {count} 다 (기대 {EXPECTED_CHUNK_COUNT}). "
            f"`08 §2` 본문이 바뀌었거나 청킹 규칙이 달라졌다 — "
            f"`scripts/probe_calibration.py` 의 SEED_CHUNKS 도 함께 확인하라."
        )
    if count <= top_k:
        raise SystemExit(f"청크 수({count})가 retrieval_top_k({top_k}) 이하다 — 검색이 no-op 다.")
    log(f"· 청크 {count}개 (retrieval_top_k={top_k})")
    return count


# --------------------------------------------------------------------------------------
# 3. 이력 (`--with-history`)
# --------------------------------------------------------------------------------------
@dataclass
class AskedQuestion:
    family: str
    content_ko: str
    question_id: UUID
    grade: str | None
    status: str
    answer_id: UUID | None
    answer_state: str | None


async def run_history(
    db: AsyncSession, project: Project, askers: list[User]
) -> list[AskedQuestion]:
    """질문 전체(`HISTORY`, 현재 123건)를 **실제 파이프라인으로** 돌린다 (`08 §5` 3번).

    질문자를 번갈아 배정한다 — "맞았다 2건"이 서로 다른 유저여야 승인 추천이 성립하기
    때문이다 (`04 §7` UNIQUE(answer_id, user_id)).
    """
    results: list[AskedQuestion] = []

    for index, item in enumerate(HISTORY):
        asker = askers[index % len(askers)]
        question = await question_service.create_question(
            db,
            project_id=project.id,
            asker=asker,
            payload=QuestionCreate(content_ko=item.content_ko, urgency="normal"),
        )
        question_id = question.id
        await db.commit()

        # 파이프라인은 **자체 세션**에서 돌고 거기서 커밋한다 (`03 §2` 원칙 4).
        # 시드는 스크립트라 백그라운드 태스크로 띄우지 않고 그대로 await 한다.
        await answer_pipeline.run_answer_pipeline(question_id)

        # ⚠️ 결과는 **컬럼 select 로** 읽는다. `db.get(Question, ...)` 은 세션의 identity map 에
        #    남아 있는 `processing` 상태 객체를 그대로 돌려줄 수 있다 (`expire_on_commit=False`).
        row = (
            await db.execute(
                select(Question.status, Answer.id, Answer.grade, Answer.state)
                .outerjoin(Answer, Answer.question_id == Question.id)
                .where(Question.id == question_id)
            )
        ).first()
        await db.commit()  # 다음 반복이 새 스냅샷을 보게 한다.

        results.append(
            AskedQuestion(
                family=item.family,
                content_ko=item.content_ko,
                question_id=question_id,
                grade=row.grade if row is not None else None,
                status=row.status if row is not None else "unknown",
                answer_id=row.id if row is not None else None,
                answer_state=row.state if row is not None else None,
            )
        )
        log(
            f"  [{index + 1:2d}/{len(HISTORY)}] {item.family:<3} "
            f"{(row.grade if row is not None else None) or '—':<6} {item.content_ko[:38]}"
        )

    return results


async def inject_feedback(db: AsyncSession, asked: list[AskedQuestion], askers: list[User]) -> int:
    """질문자 "맞았다" 주입 — `grade_accuracy` 분자의 한쪽이다 (D25).

    ⚠️ **승인과 달리 계열 제한이 없다.** correct 피드백은 공식 Q&A 를 만들지 않으므로
    (`06 §3` 편입 경로는 카드 확정뿐) 데모 당일 재사용 경로를 오염시키지 않는다. 두 건이
    모이면 카드가 `recommend_approve` 로 올라올 뿐이고, 그건 브리핑 화면에 보여 줄 재료다.
    """
    published = [item for item in asked if item.answer_id is not None and item.grade != GRADE_RED]
    injected = 0

    for index, item in enumerate(published):
        if index % FEEDBACK_EVERY >= FEEDBACK_TAKE:
            continue

        question = await db.get(Question, item.question_id)
        answer = await db.get(Answer, item.answer_id) if item.answer_id else None
        if question is None or answer is None:
            continue

        # 두 번째 "맞았다"는 3건에 1건만 — 전부 2건이면 큐 전체가 승인 추천으로 뜬다.
        voters = askers if index % 3 == 0 else askers[:1]
        for voter in voters:
            await feedback_service.submit(
                db,
                question=question,
                answer=answer,
                user=voter,
                payload=FeedbackCreate(verdict=FEEDBACK_CORRECT),
            )
            injected += 1
        await db.commit()

    log(f"· '맞았다' 피드백 {injected}건 주입")
    return injected


async def inject_approvals(
    db: AsyncSession, project: Project, asked: list[AskedQuestion], answerer: User
) -> int:
    """담당자 승인 주입 — `grade_accuracy` 분자의 나머지 한쪽이다 (D25).

    ⛔ **`NO_APPROVAL_KEYS` 계열은 제외한다** (`09 §2`). 승인은 공식 Q&A 를 만들고(`06 §3`),
    그러면 데모 당일 같은 질문이 ② 에서 재사용 경로로 빠져 `matching_rate`·`search_score`·
    인용이 전부 `null` 이 된다 (`05 §6` D11) — §4 1단계의 "🟢 87% + 인용"이 사라진다.

    ⚠️ 카드 상세를 부르지 않는다. `resolve_card` 는 `first_viewed_at` 을 건드리지 않으므로
    이 경로만으로 확정할 수 있다 (M7 `card_handle_30s_rate` 보호).
    """
    candidates = [
        item
        for item in asked
        if item.answer_id is not None
        and item.grade in (GRADE_GREEN, GRADE_YELLOW)
        and item.answer_state == ANSWER_STATE_DRAFT
        and item.family not in NO_APPROVAL_KEYS
    ]
    approved = 0

    for index, item in enumerate(candidates):
        if index % APPROVE_EVERY >= APPROVE_TAKE:
            continue

        card = await db.scalar(
            select(ReviewCard).where(ReviewCard.answer_id == item.answer_id).limit(1)
        )
        if card is None or card.status == CARD_STATUS_RESOLVED:
            continue

        await review_card_service.resolve_card(
            db, card=card, actor=answerer, action=review_card_service.ACTION_APPROVE
        )
        await db.commit()
        approved += 1

    log(f"· 담당자 승인 {approved}건 주입 (제외 계열: {', '.join(sorted(NO_APPROVAL_KEYS))})")
    return approved


async def resolve_red_cards(
    db: AsyncSession, project: Project, asked: list[AskedQuestion], answerer: User
) -> int:
    """🔴 카드 일부를 담당자가 **선택지로 확정**한다 — `grade_accuracy` 🔴 의 분자다 (D25).

    ⚠️ **이걸 안 하면 지표 화면에 "🔴 정확도 0%" 가 뜬다.** "AI 의 🔴 판정이 전부 틀렸다"로
    읽히지만 실제 뜻은 "시드가 🔴 을 아무도 확인하지 않았다" 이다. 🔴 답변은 질문자에게
    발행되지 않으므로 `correct` 피드백 경로가 없고(`accuracy_service.for_grade` 분자),
    **담당자 확정만이 분자를 만든다.** 실제 운영에서는 담당자가 인박스의 🔴 을 처리하므로
    0% 가 나올 수 없다 — 시드가 그 절반을 재현한다.

    ⛔ **`answer-option` 을 쓴다.** `approve` 는 🔴 초안이 본문 없이 남는 경우가 있어 빈 답변을
    발행하고, `edit` 은 시드가 답 내용을 **지어내야** 한다. 선택지는 ⑦ 구조화가 만든 것이라
    둘 다 피하면서 `05 §7.2` 의 "30초 컷" 경로를 그대로 탄다.

    ⚠️ 카드 상세를 부르지 않는다 — `first_viewed_at` 오염 금지 (M7 `card_handle_30s_rate`).
    """
    candidates = [
        item
        for item in asked
        if item.answer_id is not None
        and item.grade == GRADE_RED
        and item.answer_state == ANSWER_STATE_DRAFT
        and item.family not in NO_RED_RESOLVE_KEYS
    ]
    resolved = 0

    for index, item in enumerate(candidates):
        if index % RED_RESOLVE_EVERY >= RED_RESOLVE_TAKE:
            continue

        # ⚠️ `project_id` 로 한정하고 순서를 고정한다 — 한 답변에 카드가 여럿일 수 있고,
        #    정렬 없는 limit(1) 은 물리적 행 배치에 따라 다른 카드를 집는다 (`04 §7`).
        card = await db.scalar(
            select(ReviewCard)
            .where(
                ReviewCard.answer_id == item.answer_id,
                ReviewCard.project_id == project.id,
            )
            .order_by(ReviewCard.created_at)
            .limit(1)
        )
        if card is None or card.status == CARD_STATUS_RESOLVED:
            continue

        # ⚠️ **소비자와 같은 객체를 읽는다.** `review_card_service._selected_option` 은
        #    `card.question_struct` 를 보므로 여기서 `answer.question_struct` 를 검사하면
        #    둘이 어긋났을 때 가드가 헛돈다.
        options = (card.question_struct or {}).get("options")
        if not options:
            continue

        try:
            await review_card_service.resolve_card(
                db,
                card=card,
                actor=answerer,
                action=review_card_service.ACTION_ANSWER_OPTION,
                option_index=0,
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — 한 건 실패로 25분치 시드를 잃지 않는다.
            await db.rollback()
            log(f"  · 🔴 카드 확정 건너뜀 (card={card.id}): {type(exc).__name__} {exc}")
            continue
        resolved += 1

    log(f"· 🔴 카드 {resolved}건 확정 (제외 계열: {', '.join(sorted(NO_RED_RESOLVE_KEYS))})")
    return resolved


async def run_reuse_followups(
    db: AsyncSession, project: Project, askers: list[User], guarded_texts: set[str]
) -> int:
    """확정된 공식 Q&A 를 **다시 묻는** 질문을 태워 ② 재사용 경로를 이력에 남긴다.

    ⚠️ 이 함수가 없으면 이력에 재사용이 **0건**이 된다. 이유는 버그가 아니라 순서다 —
    `run_history` 가 질문 전체를 먼저 돌리고 `inject_approvals` 가 그 뒤에 공식 Q&A 를
    만들기 때문에, 질문들이 돌던 시점에는 재사용할 지식이 아직 없었다. 지식이 쌓여
    재활용된다는 것이 이 제품의 요지인데 이력 화면과 지표에서 그게 안 보였다.

    ⛔ **라이브 시연이 던질 질문과 겹치는 공식 Q&A 는 건드리지 않는다.** 원문을 다시 물어
    또 하나의 재사용 이력을 만들면 문제가 없지만, 그 대상이 Q8·Q10 계열이면 발표 당일
    시나리오 B 가 통째로 사라진다 (`09 §2`). `guarded_texts` 가 그 방어선이다.

    질문자는 **원 질문자와 다른 사람**으로 고른다 — "다른 사람이 같은 걸 또 물었다"가
    재사용이 성립하는 실제 상황이고, 같은 사람이 같은 질문을 두 번 하는 그림은 어색하다.
    """
    official = (
        await db.scalars(
            select(OfficialQA)
            .where(OfficialQA.project_id == project.id)
            .order_by(OfficialQA.created_at)
        )
    ).all()

    targets = [qa for qa in official if qa.question_ko not in guarded_texts][:REUSE_FOLLOWUP_COUNT]
    reused = 0

    for index, qa in enumerate(targets):
        asker = askers[(index + 1) % len(askers)]
        question = await question_service.create_question(
            db,
            project_id=project.id,
            asker=asker,
            payload=QuestionCreate(content_ko=qa.question_ko, urgency="normal"),
        )
        question_id = question.id
        await db.commit()

        await answer_pipeline.run_answer_pipeline(question_id)

        row = (
            await db.execute(
                select(Answer.source, Answer.state)
                .join(Question, Question.id == Answer.question_id)
                .where(Question.id == question_id)
            )
        ).first()
        await db.commit()

        if row is not None and row.source == ANSWER_SOURCE_REUSED:
            reused += 1
        log(f"  [{index + 1}/{len(targets)}] 재질문 → source={row.source if row else 'none'}")

    log(f"· 재질문 {len(targets)}건 중 재사용 {reused}건")
    return reused


async def assert_live_questions_not_reusable(db: AsyncSession, project: Project) -> None:
    """`09 §2` 승인 주입 어서션 — 명시적 제외 목록의 **추가 안전망**.

    제외 목록은 "우리가 의도한 것"만 막는다. 여기서는 결과를 본다: 라이브 시연이 던지는
    Q1·Q8·Q10 **원문**을 ② 재사용 검사와 같은 축(질문의 영어 번역문 임베딩)으로 조회해
    가장 가까운 공식 Q&A 의 원시 코사인이 `similar_threshold` 미만인지 확인한다.

    `similar_threshold`(재사용보다 낮은 문턱)를 쓰는 이유는 `09 §2` 가 그렇게 못박았기
    때문이다 — 이 값을 넘으면 재사용까지 가지 않더라도 "비슷한 확정 답변"이 화면에 첨부되고
    (D24), 데모 대사가 흔들린다.

    ⚠️ 임계값은 `projects.settings` 에서 읽는다. 하드코딩 금지 (룰 3).
    """
    provider = get_provider()
    threshold = float(project.settings["similar_threshold"])

    # ⚠️ `NO_APPROVAL_KEYS` 가 아니라 **`NO_RED_RESOLVE_KEYS`** 다 — `resolve_red_cards` 가
    #    🔴 계열에서도 공식 Q&A 를 만들므로, 승인 제외 목록만 보면 Q7 이 무방비가 된다.
    guarded = [BY_KEY[key] for key in sorted(NO_RED_RESOLVE_KEYS)]
    # ① 과 같은 프롬프트·스키마로 영어 번역문을 만든다 — 축이 다르면 어서션이 무의미하다.
    english: list[str] = []
    for question in guarded:
        translated = await provider.complete_json(
            prompts.TRANSLATE_SYSTEM,
            question.content_ko,
            TranslationOut,
            model=settings.llm_model_translate,
        )
        english.append(translated["content_en"])

    vectors = await provider.embed(english)

    for question, vector in zip(guarded, vectors, strict=True):
        candidate = await retrieval.search_official_qa(
            db, project_id=project.id, query_embedding=vector
        )
        best = candidate[1] if candidate is not None else 0.0
        if best >= threshold:
            raise SystemExit(
                f"{question.key} 원문이 공식 Q&A 와 너무 가깝다 (sim_raw={best:.4f} ≥ "
                f"similar_threshold={threshold}). 승인 주입이 라이브 시연 경로를 오염시켰다 "
                f"— `09 §2` 제외 목록을 확인하라.\n"
                f"  project_id={project.id} · **데이터는 남아 있다** — 질문셋을 고친 뒤 "
                f"`--reset --with-history` 로 다시 채워라."
            )
        log(f"  · {question.key} 원문 ↔ 최근접 공식 Q&A sim_raw={best:.4f} < {threshold} ✓")


def report_grades(asked: list[AskedQuestion]) -> int:
    """등급 분포 보고. 돌려주는 값은 🟢 발행 건수다."""
    grades = Counter(item.grade or "none" for item in asked)
    log("")
    log("── 등급 분포 ──────────────────────────────────────────")
    for grade in (GRADE_GREEN, GRADE_YELLOW, GRADE_RED, "none"):
        if grades.get(grade):
            log(f"  {grade:<7} {grades[grade]:3d}건")

    # ⚠️ 판정 기준은 `GREEN_EXPECTED_FAMILIES` 가 아니라 **계열의 `expected_grades` 정본**이다.
    #    Q5 는 `08 §3` 이 🟢~🟡 **둘 다 정답**으로 적어 둔 계열이라(단일 출처라 근거가 얇다)
    #    🟡 이 나오는 것이 사양대로다. 평평한 집합으로 세면 발표 전날 밤에 "Q5 → yellow" 라는
    #    **없는 문제**를 쫓게 된다.
    off_spec = Counter(
        (item.family, item.grade or "none")
        for item in asked
        if item.grade not in BY_KEY[item.family].expected_grades
    )
    if off_spec:
        log("  기대 등급 밖으로 나온 계열 (`08 §3` 표 기준):")
        for (family, grade), count in sorted(off_spec.items()):
            expected = "|".join(BY_KEY[family].expected_grades)
            log(f"    {family:<4} → {grade:<7} {count}건 (기대 {expected})")

    return grades.get(GRADE_GREEN, 0)


# --------------------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------------------
async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="데모 시드 (`08 §5`)")
    parser.add_argument("--reset", action="store_true", help="기존 데모 데이터를 지우고 재주입")
    parser.add_argument(
        "--with-history",
        action="store_true",
        help=(
            f"질문 {len(HISTORY)}건을 실제 파이프라인으로 돌려 이력·지표를 채운다 "
            f"(실 LLM 전제 · 약 {len(HISTORY) * 3}~{len(HISTORY) * 4} 호출 · 25~30분)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.with_history and settings.llm_provider != "openai":
        log("")
        log("⚠️  LLM_PROVIDER 가 openai 가 아니다 — FakeLLM 의 임베딩은 해시 기반이라")
        log("    질문↔청크 코사인이 사실상 0 이고 similarity_floor 에 전부 걸린다.")
        log(
            f"    {len(HISTORY)}건이 **전부 강제 🔴 no_evidence** 로 나온다. "
            "스크립트 점검용으로만 쓸 것."
        )
        log("")

    async with AsyncSessionLocal() as db:
        if args.reset:
            await reset(db)
        elif await db.scalar(select(Project.id).where(Project.name == PROJECT_NAME)):
            raise SystemExit(
                f"'{PROJECT_NAME}' 프로젝트가 이미 있다. 재주입하려면 --reset 을 붙여라."
            )

        answerer = await ensure_user(db, ANSWERER)
        askers = [await ensure_user(db, spec) for spec in ASKERS]
        await db.commit()
        log(f"· 유저 {1 + len(askers)}명 준비 (비밀번호 {DEMO_PASSWORD})")

        project = await build_project(db, answerer, askers)
        await upload_seed_documents(db, project, answerer)
        await assert_chunk_count(db, project)

        if not args.with_history:
            log("")
            log(f"✅ 시드 완료 — project_id={project.id}")
            log("   이력·지표까지 채우려면 --with-history 로 다시 실행하라 (실 LLM 비용 발생).")
            return 0

        log("")
        log(f"── 이력 {len(HISTORY)}건 실행 ────────────────────────────")
        asked = await run_history(db, project, askers)
        await inject_feedback(db, asked, askers)
        await inject_approvals(db, project, asked, answerer)
        await resolve_red_cards(db, project, asked, answerer)

        log("")
        log("── 재질문(② 재사용 경로) ──────────────────────────────")
        # 라이브 시연이 쓰는 계열은 원문째로 제외한다 — 여기서 공식 Q&A 를 소비하면
        # 발표 당일 시나리오가 재사용으로 빠진다 (`run_reuse_followups` 독스트링).
        guarded_texts = {BY_KEY[key].content_ko for key in NO_RED_RESOLVE_KEYS}
        await run_reuse_followups(db, project, askers, guarded_texts)

        log("")
        log("── 라이브 시연 경로 보호 확인 (`09 §2`) ────────────────")
        await assert_live_questions_not_reusable(db, project)

        green = report_grades(asked)
        answered = sum(1 for item in asked if item.status == QUESTION_STATUS_ANSWERED)
        summary = f"질문 {len(asked)}건 · 발행 {answered}건 · 🟢 {green}건"
        log("")

        if green < MIN_GREEN_SAMPLE:
            log(f"❌ 시드는 주입됐지만 🟢 표본이 {green}건이라 D25 기준({MIN_GREEN_SAMPLE}건)에")
            log("   못 미친다 — `grade_accuracy` 가 전 등급 '표본 부족'으로 떠서 발표 마지막")
            log("   화면이 빈다. 데이터는 그대로 두었다. 질문셋(scripts/demo_questions.py)의")
            log("   🟢 계열 변형을 늘린 뒤 --reset --with-history 로 다시 채워라.")
            log(f"   project_id={project.id} · {summary}")
            return 1

        log(f"✅ 시드 완료 — project_id={project.id}")
        log(f"   {summary}")

        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
