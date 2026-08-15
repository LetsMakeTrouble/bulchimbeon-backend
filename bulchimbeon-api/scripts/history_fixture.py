"""이력 픽스처 — **한 번 진짜로 돌린 결과**를 파일로 굳혀 git 에 올린다.

```bash
# 1) 이력을 한 번 실제로 채운다 (실 LLM · 25~30분 · 약 $1.2)
uv run python scripts/seed.py --reset --with-history --profile bulchimbeon
# 2) 그 결과를 픽스처로 내보낸다 (git 에 올린다)
uv run python scripts/history_fixture.py --profile bulchimbeon
# 3) 이후로는 누가 어디서 시드하든 0원·수 초·완전히 같은 데모다
uv run python scripts/seed.py --reset --profile bulchimbeon --from-fixture
```

> ### 왜 손으로 짓지 않고 내보내는가
> 매칭률·등급·정확도는 발표에서 **우리 파이프라인의 실측**으로 읽힌다. 손으로 적으면 그
> 화면이 근거를 잃는다. 어차피 코퍼스가 성립하는지 보려면 한 번은 진짜로 돌려야 하므로,
> 그 결과를 버리지 않고 고정하는 것뿐이다. 발표 당일의 30분·쿼터·API 장애가 변수에서
> 사라지는 것은 덤이다.

> ### 옮기지 않는 것
> - **`llm_usage`** — `quota.used` 는 **오늘 날짜**로 센다. 오늘자 사용량 행을 심으면
>   라이브 질문이 상한(`daily_llm_call_limit`)에 걸린다.
> - **임베딩 벡터** — `official_qas.question_embedding` 은 임포트 때 **다시 만든다**.
>   벡터를 파일에 박아 두면 나중에 임베딩 모델이 바뀌었을 때 살아 있는 질문과 축이
>   어긋나 재사용이 조용히 안 걸린다. 입력은 `question_en` 컬럼이라 재현이 정확하다
>   (`official_qa_service` 가 임베딩하는 텍스트와 같다).
> - **`briefing_runs`** — `UNIQUE(project_id, run_date)` 라 날짜를 밀면 충돌한다.

> ### 시간은 상대값으로 적는다
> 지표는 `now() - 30일` 창으로 읽는다 (`accuracy_service`·`metrics_service`). 절대 시각을
> 그대로 심으면 **한 달 뒤에 지표 화면이 빈다.** 그래서 각 행의 시각을 "가장 마지막
> 항목으로부터 몇 초 전"으로 적고, 임포트가 그것을 지금 기준으로 되돌린다.

⚠️ **모르는 외래 키를 만나면 내보내기를 멈춘다.** 스키마가 자라 새 참조 컬럼이 생겼는데
여기 규칙이 없으면, 조용히 남의 프로젝트를 가리키는 UUID 를 심는 것보다 멈추는 편이 낫다.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pgvector.sqlalchemy import Vector  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models.document import Chunk, Document, DocumentVersion  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.official_qa import OfficialQA  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.question import Answer, AnswerCitation, Question  # noqa: E402
from app.models.review_card import Feedback, ReviewCard  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.llm import get_provider  # noqa: E402
from scripts import demo_profiles  # noqa: E402
from scripts.demo_profiles import DemoProfile  # noqa: E402

FORMAT_VERSION = 1
FIXTURE_FILENAME = "history.json"

# 삽입 순서 = FK 의존 순서. `answers.official_qa_id` 만 순환이라 뒤에서 따로 채운다.
TABLES: tuple[type, ...] = (
    Question,
    Answer,
    AnswerCitation,
    OfficialQA,
    ReviewCard,
    Feedback,
    Event,
    Notification,
)

# `answers ↔ official_qas` 순환 FK (`04 §2`). 삽입 시 NULL 로 두고 마지막에 UPDATE 한다.
DEFERRED: dict[str, tuple[str, ...]] = {
    "answers": ("official_qa_id", "similar_official_qa_id"),
}

# FK 가 **없는** UUID 컬럼. `events.entity_id` 는 무엇을 가리키는지 `entity_type` 이 정하는
# 다형 참조라 스키마에 제약이 없다.
#
# ⚠️ 아는 행이면 ref 로 바꾸고, 모르는 행(문서·버전처럼 픽스처가 안 옮기는 것)이면 원래
#    UUID 를 그대로 둔다. 지우면 그 이벤트가 타임라인에서 대상을 잃고, 억지로 다른 행에
#    붙이면 없는 이력을 만든다. 매달리지 않는 편이 정직하다.
POLYMORPHIC_UUID: dict[str, tuple[str, ...]] = {"events": ("entity_id",)}

# 시드가 **스스로 다시 찍는** 이벤트의 대상들. 픽스처는 이력만 들고 있어야 한다.
#
# ⚠️ 안 걸러내면 임포트한 프로젝트의 이벤트가 이만큼 **중복**된다 — 프로젝트 생성·초대
#    참여·문서 업로드는 `--from-fixture` 경로에서도 실제로 다시 일어나기 때문이다.
#    (실측 2026-08-16: `document.version_activated` 5건 + `member.joined` 3건이 겹쳤다.)
SETUP_ENTITY_TYPES = ("project", "project_member", "document", "document_version", "guideline")

# 프로젝트 스코프 컬럼 — 파일에 적지 않고 임포트가 새 프로젝트 id 로 채운다.
PROJECT_COLUMN = "project_id"

SKIPPED_COLUMNS = frozenset({"id", "updated_at"})

_TABLE_KEYS = {model.__tablename__ for model in TABLES}


@dataclass(frozen=True)
class ChunkKey:
    """청크를 서버 간에 이어 주는 좌표. UUID 는 인제스트마다 새로 생긴다."""

    filename: str
    version_no: int
    seq: int

    def as_json(self) -> list[Any]:
        return [self.filename, self.version_no, self.seq]


def _fk_target(model: type, column_name: str) -> str | None:
    column = model.__table__.columns[column_name]
    for fk in column.foreign_keys:
        return fk.column.table.name
    return None


# --------------------------------------------------------------------------------------
# 내보내기
# --------------------------------------------------------------------------------------
async def dump(db: AsyncSession, profile: DemoProfile) -> dict[str, Any]:
    project = await db.scalar(select(Project).where(Project.name == profile.project_name))
    if project is None:
        raise SystemExit(
            f"'{profile.project_name}' 프로젝트가 없다 — 먼저 --with-history 로 채워라."
        )

    users = {user.id: user.email for user in (await db.scalars(select(User))).all()}
    chunks = await _chunk_keys(db, project.id)

    rows: dict[str, list[dict[str, Any]]] = {}
    refs: dict[UUID, str] = {}

    # 1차: 행을 읽고 ref 이름을 붙인다. payload 안의 UUID 를 바꾸려면 전체 지도가 먼저 필요하다.
    raw: dict[str, list[Any]] = {}
    for model in TABLES:
        key = model.__tablename__
        items = list((await db.scalars(_select_for(model, project.id))).all())
        raw[key] = items
        for index, item in enumerate(items):
            refs[item.id] = f"{key}:{index}"

    anchor = max(
        (item.created_at for items in raw.values() for item in items),
        default=None,
    )
    if anchor is None:
        raise SystemExit("내보낼 행이 없다 — 이력이 비어 있다.")

    for model in TABLES:
        key = model.__tablename__
        rows[key] = [
            _dump_row(model, item, anchor=anchor, users=users, chunks=chunks, refs=refs)
            for item in raw[key]
        ]

    return {
        "format": FORMAT_VERSION,
        "profile": profile.key,
        "project_name": profile.project_name,
        "counts": {key: len(value) for key, value in rows.items()},
        "rows": rows,
    }


def _select_for(model: type, project_id: UUID) -> Any:
    """프로젝트 스코프 셀렉트. 프로젝트 컬럼이 없는 테이블은 부모를 타고 좁힌다."""
    if PROJECT_COLUMN in model.__table__.columns:
        stmt = select(model).where(model.project_id == project_id)
    elif model is Answer:
        stmt = select(model).join(Question, Question.id == Answer.question_id).where(
            Question.project_id == project_id
        )
    elif model is AnswerCitation:
        stmt = (
            select(model)
            .join(Answer, Answer.id == AnswerCitation.answer_id)
            .join(Question, Question.id == Answer.question_id)
            .where(Question.project_id == project_id)
        )
    elif model is Feedback:
        stmt = (
            select(model)
            .join(Answer, Answer.id == Feedback.answer_id)
            .join(Question, Question.id == Answer.question_id)
            .where(Question.project_id == project_id)
        )
    else:  # 새 테이블을 TABLES 에 넣고 여기를 안 고친 경우다.
        raise SystemExit(f"{model.__tablename__}: 프로젝트로 좁히는 방법이 정의되지 않았다.")

    if model is Event:
        stmt = stmt.where(
            Event.entity_type.is_(None) | Event.entity_type.notin_(SETUP_ENTITY_TYPES)
        )

    return stmt.order_by(model.created_at, model.id)


async def _chunk_keys(db: AsyncSession, project_id: UUID) -> dict[UUID, ChunkKey]:
    result = await db.execute(
        select(Chunk.id, DocumentVersion.original_filename, DocumentVersion.version_no, Chunk.seq)
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.project_id == project_id)
    )
    return {row[0]: ChunkKey(row[1], row[2], row[3]) for row in result.all()}


def _dump_row(
    model: type,
    item: Any,
    *,
    anchor: datetime,
    users: dict[UUID, str],
    chunks: dict[UUID, ChunkKey],
    refs: dict[UUID, str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, column in model.__table__.columns.items():
        if name in SKIPPED_COLUMNS or name == PROJECT_COLUMN:
            continue
        if isinstance(column.type, Vector):
            continue  # 임포트가 다시 만든다 (모듈 독스트링).

        value = getattr(item, name)
        if value is None:
            out[name] = None
            continue

        if isinstance(value, datetime):
            out[name] = {"__ago": (anchor - value).total_seconds()}
            continue

        target = _fk_target(model, name)
        if target == "users":
            out[name] = {"__user": users[value]}
        elif target == "chunks":
            out[name] = {"__chunk": chunks[value].as_json()}
        elif target in _TABLE_KEYS:
            out[name] = {"__ref": refs[value]}
        elif target == "document_versions":
            # 인제스트 실패 카드만 갖는 값이다. 데모 이력에는 없으므로 옮기지 않는다.
            out[name] = None
        elif target is not None:
            raise SystemExit(
                f"{model.__tablename__}.{name} 이 '{target}' 를 가리킨다 — 규칙이 없다. "
                "history_fixture.py 에 처리 규칙을 더한 뒤 다시 내보내라."
            )
        elif isinstance(value, (dict, list)):
            out[name] = _replace_uuids(value, refs)
        elif isinstance(value, UUID):
            if name not in POLYMORPHIC_UUID.get(model.__tablename__, ()):
                raise SystemExit(
                    f"{model.__tablename__}.{name}: FK 가 아닌 UUID 다 — 규칙이 없다. "
                    "다형 참조라면 POLYMORPHIC_UUID 에 등록하고, 아니라면 왜 UUID 인지 "
                    "확인한 뒤 규칙을 더해라."
                )
            ref = refs.get(value)
            out[name] = {"__ref": ref} if ref else {"__uuid": str(value)}
        else:
            out[name] = value
    return out


def _replace_uuids(value: Any, refs: dict[UUID, str]) -> Any:
    """JSON 안에 문자열로 박힌 UUID 를 ref 로 바꾼다.

    ⚠️ 지표가 `events.payload['card_id']` 로 카드를 되짚는다 (`metrics_service`). 이걸
    안 바꾸면 임포트한 이력에서 **카드 30초 처리율이 통째로 0** 이 된다 — 화면은 멀쩡히
    뜨고 숫자만 틀린다.
    """
    if isinstance(value, dict):
        return {key: _replace_uuids(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_uuids(item, refs) for item in value]
    if isinstance(value, str):
        try:
            ref = refs.get(UUID(value))
        except ValueError:
            return value
        return f"__ref:{ref}" if ref else value
    return value


# --------------------------------------------------------------------------------------
# 불러오기
# --------------------------------------------------------------------------------------
async def load(db: AsyncSession, profile: DemoProfile, project: Project, path: Path) -> int:
    """픽스처를 프로젝트에 심는다. 돌려주는 값은 심은 행 수다."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT_VERSION:
        raise SystemExit(f"모르는 픽스처 포맷: {payload.get('format')} (기대 {FORMAT_VERSION})")
    if payload.get("profile") != profile.key:
        raise SystemExit(
            f"픽스처는 '{payload.get('profile')}' 프로필의 것이다 — 지금은 '{profile.key}' 다."
        )

    rows: dict[str, list[dict[str, Any]]] = payload["rows"]
    users = {email: user_id for user_id, email in (await db.execute(select(User.id, User.email)))}
    chunks = {key: chunk_id for chunk_id, key in (await _chunk_keys(db, project.id)).items()}

    # id 를 먼저 전부 배정한다 — 그러면 참조 해석에 순서 문제가 없다.
    ids = {
        f"{key}:{index}": uuid4() for key, items in rows.items() for index in range(len(items))
    }
    now = datetime.now().astimezone()
    deferred: list[tuple[Any, str, UUID]] = []
    planted = 0

    # ⚠️ `official_qas.question_embedding` 은 NOT NULL 이다 — 행을 먼저 넣고 나중에 채울 수
    #    없다(첫 flush 에서 죽는다). 삽입 **전에** 한 번에 만들어 둔다.
    #    입력은 `question_en` — `official_qa_service` 가 임베딩하는 텍스트와 같아야 재질문이
    #    ② 재사용 경로를 탄다 (`06 §2` ②).
    official_rows = rows.get(OfficialQA.__tablename__, [])
    official_vectors = (
        await get_provider().embed([row["question_en"] for row in official_rows])
        if official_rows
        else []
    )

    for model in TABLES:
        key = model.__tablename__
        cyclic = DEFERRED.get(key, ())
        for index, row in enumerate(rows.get(key, [])):
            values: dict[str, Any] = {"id": ids[f"{key}:{index}"]}
            if PROJECT_COLUMN in model.__table__.columns:
                values[PROJECT_COLUMN] = project.id

            for name, raw_value in row.items():
                if name in cyclic:
                    continue  # 아래에서 따로 채운다.
                values[name] = _resolve(
                    raw_value, ids=ids, users=users, chunks=chunks, now=now
                )

            if model is OfficialQA:
                values["question_embedding"] = official_vectors[index]

            entity = model(**values)
            db.add(entity)
            planted += 1

            # 순환 FK(`answers ↔ official_qas`) 는 양쪽 행이 다 들어간 뒤에 채운다.
            for column_name in cyclic:
                marker = row.get(column_name)
                if isinstance(marker, dict) and "__ref" in marker:
                    deferred.append((entity, column_name, ids[marker["__ref"]]))
        await db.flush()

    for entity, column_name, target_id in deferred:
        setattr(entity, column_name, target_id)
    await db.flush()

    await db.commit()
    return planted


def _resolve(
    value: Any,
    *,
    ids: dict[str, UUID],
    users: dict[str, UUID],
    chunks: dict[ChunkKey, UUID],
    now: datetime,
) -> Any:
    if not isinstance(value, dict):
        return value
    if "__ago" in value:
        return now - timedelta(seconds=float(value["__ago"]))
    if "__ref" in value:
        return ids[value["__ref"]]
    if "__user" in value:
        email = value["__user"]
        if email not in users:
            raise SystemExit(f"픽스처가 가리키는 유저가 없다: {email} — 시드를 먼저 돌려라.")
        return users[email]
    if "__uuid" in value:
        # 픽스처 밖을 가리키던 다형 참조. 그대로 둔다 (`POLYMORPHIC_UUID` 주석).
        return UUID(value["__uuid"])
    if "__chunk" in value:
        chunk_key = ChunkKey(*value["__chunk"])
        if chunk_key not in chunks:
            raise SystemExit(
                f"픽스처가 가리키는 청크가 없다: {chunk_key} — 코퍼스가 바뀌었다면 "
                "이력을 다시 내보내야 한다."
            )
        return chunks[chunk_key]
    # payload 같은 JSON 컬럼 — 안의 ref 문자열만 되돌린다.
    return _restore_uuids(value, ids)


def _restore_uuids(value: Any, ids: dict[str, UUID]) -> Any:
    if isinstance(value, dict):
        return {key: _restore_uuids(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_uuids(item, ids) for item in value]
    if isinstance(value, str) and value.startswith("__ref:"):
        return str(ids[value.removeprefix("__ref:")])
    return value


def fixture_path(profile: DemoProfile) -> Path:
    return profile.seed_dir / FIXTURE_FILENAME


# --------------------------------------------------------------------------------------
# CLI (내보내기 전용 — 불러오기는 `seed.py --from-fixture` 다)
# --------------------------------------------------------------------------------------
async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="이력을 픽스처로 내보낸다")
    parser.add_argument(
        "--profile",
        default=demo_profiles.DEFAULT_PROFILE.key,
        choices=sorted(demo_profiles.PROFILES),
    )
    parser.add_argument("--out", help="기본값은 <프로필 코퍼스 폴더>/history.json")
    args = parser.parse_args(argv)
    profile = demo_profiles.get(args.profile)
    out = Path(args.out) if args.out else fixture_path(profile)

    async with AsyncSessionLocal() as db:
        data = await dump(db, profile)

    out.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 개행은 LF 로 고정한다 — CRLF 로 커밋되면 diff 가 통째로 바뀌고, 시드가 CRLF 를
    #    거부하는 코퍼스 폴더와 같은 자리에 놓인다.
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")

    total = sum(data["counts"].values())
    print(f"✅ {out} — {total}행")
    for key, count in sorted(data["counts"].items()):
        print(f"   {key:<18} {count:4d}")
    print("\n   git 에 올려라. 이후 시드는 `--from-fixture` 로 0원·수 초에 끝난다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
