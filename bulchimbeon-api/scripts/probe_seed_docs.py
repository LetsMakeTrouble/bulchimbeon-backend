"""보강한 `seed/*.md` 를 **데모와 분리된 프로젝트**에 인제스트하고 S 만 재본다.

`08 §3` 기대표와 어긋난 질문들의 원인이 S(검색)일 때, 근거 섹션을 고쳐 S 가 실제로
올라갔는지 **재시드 전에** 확인하는 자리다. 재시드는 30분이 들고 데모 데이터를 통째로
갈아치우므로, 문서 수정이 먹히는지를 그 전에 알아야 한다.

    uv run python scripts/probe_seed_docs.py --answerer mike@devcorp.example

파이프라인 전체가 아니라 **① 번역 + ③ 검색까지만** 돈다. 질문당 LLM 호출 1회 +
임베딩 1회라 12건을 다 돌려도 싸다. G 는 여기서 재지 않는다 — 실측 대상 4건은
이미 G=100 이고 병목이 S 뿐임이 확인됐다(`09 §7.5` 이후).

⚠️ 끝나면 만든 프로젝트를 지운다. 남기면 담당자 프로젝트 목록에 검증용이 쌓인다.
`--keep` 으로 남길 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import UploadFile  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import DEFAULT_SETTINGS  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.document import Chunk, Document, DocumentVersion  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.project import Project, ProjectMember  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import document_service  # noqa: E402
from app.services.llm import get_provider  # noqa: E402
from app.services.pipeline import grading, ingest, prompts, retrieval  # noqa: E402
from app.services.pipeline.llm_schemas import TranslationOut  # noqa: E402
from scripts.demo_questions import CANONICAL  # noqa: E402
from scripts.seed import SEED_DIR, SEED_DOCUMENTS  # noqa: E402

PROBE_PROJECT_NAME = "PROBE — 시드 문서 보강 검증"


async def _build_project(db: AsyncSession, answerer: User) -> Project:
    project = Project(
        name=PROBE_PROJECT_NAME,
        description="근거 섹션 보강 효과 측정용. 끝나면 지운다.",
        # 반복 측정을 위해 매번 다르게 만든다 — `ix_projects_invite_code` 가 UNIQUE 다.
        invite_code=f"PROBE{uuid4().hex[:7].upper()}",
        answerer_id=answerer.id,
        settings=dict(DEFAULT_SETTINGS),
    )
    db.add(project)
    await db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=answerer.id, role="answerer"))
    await db.commit()
    return project


async def _ingest(db: AsyncSession, project: Project, uploader: User) -> None:
    version_ids = []
    for filename, title in SEED_DOCUMENTS:
        payload = (SEED_DIR / filename).read_bytes()
        _, version = await document_service.create_document(
            db,
            project_id=project.id,
            uploader=uploader,
            file=UploadFile(file=BytesIO(payload), filename=filename),
            title=title,
            auto_activate=True,
        )
        version_ids.append(version.id)
    await db.commit()
    for version_id in version_ids:
        await ingest.run_ingest(version_id, activate_on_ready=True)


async def _drop(db: AsyncSession, project: Project) -> None:
    versions = (
        await db.scalars(
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.project_id == project.id)
        )
    ).all()
    if versions:
        await db.execute(delete(Chunk).where(Chunk.document_version_id.in_(versions)))
        await db.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(versions)))
    await db.execute(delete(Document).where(Document.project_id == project.id))
    await db.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
    # 인제스트가 `document.*` 이벤트를 남긴다 (룰 4 — 모든 상태 변화는 events 에 적재된다).
    # 프로젝트 행보다 먼저 지우지 않으면 외래키에 막힌다.
    await db.execute(delete(Notification).where(Notification.project_id == project.id))
    await db.execute(delete(Event).where(Event.project_id == project.id))
    await db.execute(delete(Project).where(Project.id == project.id))
    await db.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(description="보강한 시드 문서의 S 를 잰다")
    parser.add_argument("--answerer", required=True, help="담당자 이메일 (프로젝트 소유자)")
    parser.add_argument("--keep", action="store_true", help="측정용 프로젝트를 남긴다")
    parser.add_argument(
        "--project",
        help="이미 인제스트해 둔 측정 프로젝트 UUID. 주면 문서를 다시 넣지 않는다.",
    )
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        answerer = await db.scalar(select(User).where(User.email == args.answerer))
        if answerer is None:
            raise SystemExit(f"사용자를 찾지 못했다: {args.answerer}")
        if args.project:
            project = await db.get(Project, UUID(args.project))
            if project is None:
                raise SystemExit(f"프로젝트를 찾지 못했다: {args.project}")
            print(f"기존 측정 프로젝트 재사용: {project.name} ({project.id})")
        else:
            project = await _build_project(db, answerer)
            print(f"측정 프로젝트: {project.name} ({project.id})")
            await _ingest(db, project, answerer)

    settings_map = dict(DEFAULT_SETTINGS)
    floor = float(settings_map["similarity_floor"])

    async with AsyncSessionLocal() as db:
        chunks = await db.scalar(
            select(Chunk.id)
            .join(DocumentVersion)
            .join(Document)
            .where(Document.project_id == project.id)
        )
        print(f"청크 인제스트 완료 (첫 청크 {chunks})\n")

        header = f"{'키':<5}{'질문':<34}{'sim_raw':>9}{'S':>5}{'등급 하한':>10}"
        print(header)
        print("-" * len(header))

        for question in CANONICAL:
            translated = await get_provider().complete_json(
                prompts.TRANSLATE_SYSTEM,
                question.content_ko,
                TranslationOut,
                model=None,
            )
            vector = (await get_provider().embed([translated["content_en"]]))[0]
            found = await retrieval.search_evidence(
                db,
                project_id=project.id,
                query_embedding=vector,
                top_k=int(settings_map["retrieval_top_k"]),
            )
            if not found:
                print(
                    f"{question.key:<5}{question.content_ko[:32]:<34}"
                    f"{'—':>9}{'—':>5}{'검색 0건':>10}"
                )
                continue

            sim_raw = found[0].sim_raw
            score = grading.search_score(
                sim_raw,
                s_floor=float(settings_map["s_floor"]),
                s_ceil=float(settings_map["s_ceil"]),
            )
            # 차단선 아래면 강제 🔴 `no_evidence` 다 — S 가 몇이든 등급이 결정된다.
            note = "🔴 차단" if sim_raw < floor else ("🟢 가능" if score >= 80 else "🟡")
            print(
                f"{question.key:<5}{question.content_ko[:32]:<34}"
                f"{sim_raw:>9.4f}{score:>5}{note:>10}"
            )

        if not args.keep:
            await _drop(db, project)
            print("\n측정 프로젝트 삭제 완료")
        else:
            print(f"\n측정 프로젝트를 남긴다: {project.id}")


if __name__ == "__main__":
    asyncio.run(main())
