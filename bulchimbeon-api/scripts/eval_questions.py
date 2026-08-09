"""검증 질문셋 실 LLM 대조 (`prompts/09-seed-deploy.md` 3번).

```bash
set -a; . ./.env; set +a
uv run python scripts/seed.py --reset      # 먼저 시드가 있어야 한다
uv run python scripts/eval_questions.py    # Q1~Q9·Q11~Q13 을 실제로 돌려 기대값과 대조
```

M-1 게이트(`scripts/probe_calibration.py`)의 **판정 1 표와 같은 열**로 찍는다 — 임계값이
실측 이후에도 유효한지 확인하는 마지막 관문이므로 두 출력을 나란히 놓고 읽을 수 있어야 한다.
차이는 하나뿐이다: M-1 은 검색 단계만 재고(S 만), 여기는 **파이프라인 전 구간**을 돌리므로
`min(S, G)` 로 확정된 등급과 `elapsed_ms` 가 함께 나온다.

> ### ⚠️ 실 API 비용이 발생한다
> 12건 × 3~4회 = 약 **36~48 호출**. `daily_llm_call_limit`(500) 안이지만 반복 실행은 피한다.
> `LLM_PROVIDER=fake` 로는 의미가 없다 — 해시 임베딩이라 전부 강제 🔴 이 된다 (`06 §5`).

> ### ⚠️ 기본값은 **측정 후 원상복구**다
> 이 스크립트가 만든 질문·답변·카드·이벤트를 끝에서 지운다. 남기면 (a) 담당자 큐에 데모와
> 무관한 카드가 쌓이고 (b) `GET /metrics` 의 자동응답률·시계열에 리허설이 섞인다.
> 남기고 싶으면 `--keep`.
>
> **Q10 은 여기서 돌리지 않는다.** Q10 의 기대값(🟢 재사용)은 Q8 이 **담당자 확정을 거쳐**
> 공식 Q&A 가 된 뒤에만 성립한다. 그 확정을 스크립트가 대신 하면 일본 환불 정책 Q&A 가
> 미리 만들어져 데모 당일 Q8 이 재사용 경로로 빠진다 — 시나리오 B 가 통째로 사라진다.
> Q10 재사용은 `tests/test_demo_scenarios.py` 가 FakeLLM 으로, 라이브 리허설이 실제로 본다.
"""

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.project import (  # noqa: E402
    MEMBER_STATUS_ACTIVE,
    ROLE_ASKER,
    Project,
    ProjectMember,
)
from app.models.question import (  # noqa: E402
    Answer,
    AnswerCitation,
    Question,
)
from app.models.review_card import Feedback, ReviewCard  # noqa: E402
from app.models.user import User  # noqa: E402
from app.schemas.question import QuestionCreate  # noqa: E402
from app.services import event_service, question_service  # noqa: E402
from app.services.pipeline import answer as answer_pipeline  # noqa: E402
from app.services.pipeline import dnd  # noqa: E402
from scripts.demo_questions import CANONICAL, DemoQuestion  # noqa: E402
from scripts.seed import PROJECT_NAME  # noqa: E402

# Q10 은 담당자 확정을 전제한다 — 모듈 독스트링 참조.
SKIPPED_KEYS = frozenset({"Q10"})


def log(message: str = "") -> None:
    print(message, flush=True)


def table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> None:
    """`probe_calibration.py` 와 같은 모양의 표 — 두 출력을 나란히 읽을 수 있어야 한다."""
    right = right or set()
    widths = [
        max(_width(header), *(_width(row[i]) for row in rows)) for i, header in enumerate(headers)
    ]
    log("  " + "  ".join(_pad(header, widths[i], i in right) for i, header in enumerate(headers)))
    log("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        log("  " + "  ".join(_pad(cell, widths[i], i in right) for i, cell in enumerate(row)))


def _width(text: str) -> int:
    """한글은 터미널에서 두 칸이다 — 폭을 세지 않으면 표가 어긋난다."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int, align_right: bool) -> str:
    padding = " " * max(0, width - _width(text))
    return padding + text if align_right else text + padding


@dataclass
class Measurement:
    question: DemoQuestion
    question_id: UUID
    grade: str | None
    held_reason: str | None
    matching_rate: int | None
    search_score: int | None
    grounding_score: int | None
    sim_raw: float | None
    elapsed_ms: int | None
    top_heading: str
    source: str | None

    @property
    def matches_expectation(self) -> bool:
        return self.grade in self.question.expected_grades


async def _load_project(db: AsyncSession, project_id: UUID | None) -> Project:
    if project_id is not None:
        project = await db.get(Project, project_id)
        if project is None:
            raise SystemExit(f"프로젝트를 찾을 수 없다: {project_id}")
        return project

    project = await db.scalar(
        select(Project).where(Project.name == PROJECT_NAME).order_by(Project.created_at.desc())
    )
    if project is None:
        raise SystemExit(
            f"'{PROJECT_NAME}' 프로젝트가 없다. `uv run python scripts/seed.py --reset` 먼저."
        )
    return project


async def _log_dnd_status(db: AsyncSession, project: Project) -> None:
    """지금이 담당자의 DND 안인지 한 줄 찍는다 (`08 §3` 마지막 줄의 Q9 예외 때문이다).

    ⚠️ **Q9 만 시각 의존적일 수 있다.** `no_evidence` 로 잡히면 시각과 무관하지만,
    `low_confidence` 로 떨어지고 발행 가능한 문장이 남으면 DND 시간대에 🟡 로 강등된다
    (룰 6 · D2). 그때 이 스크립트는 "기대와 다름"으로 보고하는데, 그것이 **사양대로인지
    진짜 문제인지**는 이 한 줄이 있어야 구분된다. Q7·Q8 은 강제 🔴 이라 영향받지 않는다.
    """
    answerer = await db.get(User, project.answerer_id)
    if answerer is None:
        return

    inside = dnd.in_dnd_window(
        datetime.now(UTC),
        timezone_name=answerer.timezone,
        dnd_start=str(project.settings["dnd_start"]),
        dnd_end=str(project.settings["dnd_end"]),
    )
    local = datetime.now(UTC).astimezone(dnd.zone(answerer.timezone))
    log(
        f"담당자 {answerer.name} ({answerer.timezone}) 현지 {local:%H:%M} · "
        f"DND {project.settings['dnd_start']}~{project.settings['dnd_end']} → "
        f"{'안 (Q9 가 low_confidence 면 🟡 로 강등될 수 있다)' if inside else '밖'}"
    )


async def _pick_asker(db: AsyncSession, project: Project) -> User:
    user = await db.scalar(
        select(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(
            ProjectMember.project_id == project.id,
            ProjectMember.role == ROLE_ASKER,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
        .order_by(User.created_at)
    )
    if user is None:
        raise SystemExit("프로젝트에 질문자가 없다 — 시드가 깨졌다.")
    return user


async def _measure(
    db: AsyncSession, project: Project, asker: User, question: DemoQuestion
) -> Measurement:
    created = await question_service.create_question(
        db,
        project_id=project.id,
        asker=asker,
        payload=QuestionCreate(content_ko=question.content_ko, urgency="normal"),
    )
    question_id = created.id
    await db.commit()

    started = time.monotonic()
    await answer_pipeline.run_answer_pipeline(question_id)
    wall_ms = int((time.monotonic() - started) * 1000)

    # ⚠️ 컬럼 select 로 읽는다 — identity map 에 남은 `processing` 객체를 피한다.
    row = (
        await db.execute(
            select(
                Answer.grade,
                Answer.held_reason,
                Answer.matching_rate,
                Answer.search_score,
                Answer.grounding_score,
                Answer.sim_raw,
                Answer.source,
            ).where(Answer.question_id == question_id)
        )
    ).first()

    graded = await db.scalar(
        select(Event.payload).where(
            Event.entity_id == question_id,
            Event.type == event_service.EVENT_QUESTION_GRADED,
        )
    )
    top_heading = await _top_citation_heading(db, question_id)
    await db.commit()

    return Measurement(
        question=question,
        question_id=question_id,
        grade=row.grade if row else None,
        held_reason=row.held_reason if row else None,
        matching_rate=row.matching_rate if row else None,
        search_score=row.search_score if row else None,
        grounding_score=row.grounding_score if row else None,
        sim_raw=float(row.sim_raw) if row and row.sim_raw is not None else None,
        # 파이프라인이 잰 값이 정본이고(`04 §5`), 없으면 바깥에서 잰 벽시계로 채운다.
        elapsed_ms=int((graded or {}).get("elapsed_ms") or wall_ms),
        top_heading=top_heading,
        source=row.source if row else None,
    )


async def _top_citation_heading(db: AsyncSession, question_id: UUID) -> str:
    """발행된 답변이 실제로 인용한 top-1 청크의 섹션명.

    🔴 은 발행 문장이 없어 인용도 없다 — 그때는 빈 문자열이다. 근거를 아예 못 찾았는지
    (`no_evidence`) 찾고도 막혔는지(`conflict`)는 `held_reason` 이 구분한다.
    """
    row = (
        await db.execute(
            select(AnswerCitation.quote)
            .join(Answer, Answer.id == AnswerCitation.answer_id)
            .where(Answer.question_id == question_id)
            .order_by(AnswerCitation.similarity.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return "—"
    # 청크 본문은 헤딩 줄로 시작한다 (`utils/chunking._split_by_headings`).
    first_line = row.quote.splitlines()[0].strip()
    return first_line.lstrip("#").strip()[:28]


async def _purge(db: AsyncSession, question_ids: list[UUID]) -> None:
    """이 스크립트가 만든 것만 지운다 — 리허설이 지표·큐에 섞이지 않게 (`05 §13`)."""
    if not question_ids:
        return

    answer_ids = select(Answer.id).where(Answer.question_id.in_(question_ids))
    statements = [
        update(Answer)
        .where(Answer.question_id.in_(question_ids))
        .values(official_qa_id=None, similar_official_qa_id=None),
        delete(AnswerCitation).where(AnswerCitation.answer_id.in_(answer_ids)),
        delete(Feedback).where(Feedback.answer_id.in_(answer_ids)),
        delete(ReviewCard).where(ReviewCard.question_id.in_(question_ids)),
        # `notifications` 에는 `question_id` 컬럼이 없다 — 딥링크가 JSONB `payload` 에 있다
        # (`04 §2`, `05 §11`). 그래서 payload 로 찾는다.
        delete(Notification).where(
            Notification.payload["question_id"].astext.in_(
                [str(question_id) for question_id in question_ids]
            )
        ),
        delete(Answer).where(Answer.question_id.in_(question_ids)),
        delete(Event).where(Event.entity_id.in_(question_ids)),
        delete(Question).where(Question.id.in_(question_ids)),
    ]
    for statement in statements:
        await db.execute(statement)
    await db.commit()


def _report(measurements: list[Measurement]) -> int:
    log()
    log("── 판정 1 대조 — 검색 점수와 확정 등급 (`probe_calibration.py` 판정 1 표와 같은 열) ──")
    log()
    table(
        ["#", "질문(ko)", "top-1 근거", "sim_raw", "S", "G", "매칭률", "등급", "기대", "ms"],
        [
            [
                item.question.key,
                item.question.content_ko[:30],
                item.top_heading,
                f"{item.sim_raw:.4f}" if item.sim_raw is not None else "—",
                str(item.search_score if item.search_score is not None else "—"),
                str(item.grounding_score if item.grounding_score is not None else "—"),
                str(item.matching_rate if item.matching_rate is not None else "—"),
                f"{item.grade or '—'}{'/' + item.held_reason if item.held_reason else ''}",
                "|".join(item.question.expected_grades),
                str(item.elapsed_ms),
            ]
            for item in measurements
        ],
        right={3, 4, 5, 6, 9},
    )

    mismatched = [item for item in measurements if not item.matches_expectation]
    log()
    log(f"  일치 {len(measurements) - len(mismatched)}/{len(measurements)}")

    if mismatched:
        log()
        log("  ⚠️ 기대와 다른 건:")
        for item in mismatched:
            log(
                f"    {item.question.key}: {item.grade}"
                f"{'/' + item.held_reason if item.held_reason else ''} "
                f"(기대 {'|'.join(item.question.expected_grades)}) — {item.question.note}"
            )

    # 데드라인 대조 (`06 §0` M-1 실측 p90). 🔴 은 ⑦ 구조화가 붙어 예산이 다르다.
    log()
    over = [
        item
        for item in measurements
        if item.elapsed_ms
        > (
            settings.llm_pipeline_deadline_red_seconds
            if item.grade == "red"
            else settings.llm_pipeline_deadline_seconds
        )
        * 1000
    ]
    slowest = max(measurements, key=lambda item: item.elapsed_ms)
    log(
        f"  지연: 최대 {slowest.elapsed_ms}ms ({slowest.question.key}) · "
        f"데드라인 초과 {len(over)}건 "
        f"(🟢🟡 {settings.llm_pipeline_deadline_seconds}s · "
        f"🔴 {settings.llm_pipeline_deadline_red_seconds}s)"
    )
    for item in over:
        log(f"    ❌ {item.question.key} {item.elapsed_ms}ms")

    return len(mismatched) + len(over)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검증 질문셋 실 LLM 대조 (`09 §3`)")
    parser.add_argument("--project-id", type=UUID, default=None, help="기본값은 데모 프로젝트")
    parser.add_argument(
        "--keep", action="store_true", help="측정용 질문을 지우지 않고 남긴다 (지표에 섞인다)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if settings.llm_provider != "openai":
        raise SystemExit(
            f"LLM_PROVIDER={settings.llm_provider!r} — 실 LLM 대조가 아니다. "
            "해시 임베딩으로는 전부 강제 🔴 이 된다 (`06 §5`)."
        )
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY 가 비어 있다. `set -a; . ./.env; set +a` 를 먼저.")

    targets = [question for question in CANONICAL if question.key not in SKIPPED_KEYS]

    async with AsyncSessionLocal() as db:
        project = await _load_project(db, args.project_id)
        asker = await _pick_asker(db, project)

        log(f"프로젝트: {project.name} ({project.id})")
        log(
            f"모델: answer={settings.llm_model_answer} verify={settings.llm_model_verify} "
            f"translate={settings.llm_model_translate} effort={settings.llm_reasoning_effort}"
        )
        log(f"질문 {len(targets)}건 (건너뜀: {', '.join(sorted(SKIPPED_KEYS))} — 담당자 확정 전제)")
        await _log_dnd_status(db, project)
        log()

        measurements: list[Measurement] = []
        for index, question in enumerate(targets, start=1):
            measurement = await _measure(db, project, asker, question)
            measurements.append(measurement)
            mark = "✓" if measurement.matches_expectation else "✗"
            log(
                f"  [{index:2d}/{len(targets)}] {mark} {question.key:<4} "
                f"{measurement.grade or '—':<6} {measurement.elapsed_ms:>6}ms  "
                f"{question.content_ko[:32]}"
            )

        problems = _report(measurements)

        if args.keep:
            log()
            log("  ⚠️ --keep — 측정용 질문을 남겼다. 데모 지표·큐에 섞인다.")
        else:
            await _purge(db, [item.question_id for item in measurements])
            log()
            log("  측정용 질문·답변·카드·이벤트를 원상복구했다 (--keep 으로 남길 수 있다).")

        return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
