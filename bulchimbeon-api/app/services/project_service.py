"""프로젝트·멤버·지침 서비스 (`05 §3`, `02 §9`).

커밋은 라우터가 한다 (요청 하나 = 트랜잭션 하나). 여기서는 flush 까지만 한다.
"""

import secrets
from copy import deepcopy
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.core.errors import (
    ForbiddenRole,
    InternalError,
    InviteAlreadyJoined,
    NotFound,
    ValidationError,
)
from app.models.project import (
    MEMBER_STATUS_ACTIVE,
    MEMBER_STATUS_LEFT,
    ROLE_ANSWERER,
    ROLE_ASKER,
    Guideline,
    Project,
    ProjectMember,
)
from app.models.user import User
from app.schemas.project import MemberOut, ProjectCreate, ProjectDetail, SettingsPatch
from app.services.event_service import (
    EVENT_ANSWERER_TRANSFERRED,
    EVENT_MEMBER_JOINED,
    record_event,
)

# 사람이 받아 적는 코드다 — 혼동하기 쉬운 I·O·0·1 을 뺀 알파벳을 쓴다.
_INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_INVITE_CODE_LENGTH = 8
_INVITE_CODE_MAX_ATTEMPTS = 10


async def _generate_unique_invite_code(db: AsyncSession) -> str:
    """UNIQUE 충돌은 32^8 에서 사실상 없지만, 있으면 500 이 되므로 재시도로 흡수한다."""
    for _ in range(_INVITE_CODE_MAX_ATTEMPTS):
        code = "".join(secrets.choice(_INVITE_CODE_ALPHABET) for _ in range(_INVITE_CODE_LENGTH))
        taken = await db.scalar(select(Project.id).where(Project.invite_code == code))
        if taken is None:
            return code
    raise InternalError("초대 코드를 생성하지 못했습니다.")


async def load_project(db: AsyncSession, project_id: UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFound()
    return project


def to_detail(project: Project, member: ProjectMember) -> ProjectDetail:
    """상세 응답. `invite_code` 는 담당자에게만 내린다 (초대 권한은 담당자만 — 기능 6.2)."""
    return ProjectDetail(
        id=project.id,
        name=project.name,
        description=project.description,
        role=member.role,
        member_status=member.status,
        away_mode=project.away_mode,
        settings=project.settings,
        invite_code=project.invite_code if member.role == ROLE_ANSWERER else None,
        created_at=project.created_at,
    )


async def create_project(db: AsyncSession, user: User, payload: ProjectCreate) -> ProjectDetail:
    """생성자가 곧 담당자다 (D1).

    `settings` 는 `DEFAULT_SETTINGS` 를 **복사**해 넣는다. 참조를 그대로 쓰면 한 프로젝트의
    설정 변경이 프로세스 전역 기본값을 오염시킨다.
    """
    project = Project(
        name=payload.name,
        description=payload.description,
        invite_code=await _generate_unique_invite_code(db),
        answerer_id=user.id,
        settings=deepcopy(DEFAULT_SETTINGS),
    )
    db.add(project)
    await db.flush()

    member = ProjectMember(
        project_id=project.id,
        user_id=user.id,
        role=ROLE_ANSWERER,
        status=MEMBER_STATUS_ACTIVE,
    )
    db.add(member)
    await db.flush()

    await record_event(
        db,
        project_id=project.id,
        type=EVENT_MEMBER_JOINED,
        actor_id=user.id,
        entity_type="project_member",
        entity_id=member.id,
        payload={"role": ROLE_ANSWERER, "via": "create"},
    )

    await db.refresh(project)
    return to_detail(project, member)


async def join_by_invite_code(db: AsyncSession, user: User, invite_code: str) -> ProjectDetail:
    """초대 코드로 asker 참여. 이미 활성 멤버면 409 `INVITE_ALREADY_JOINED`."""
    project = await db.scalar(select(Project).where(Project.invite_code == invite_code))
    if project is None:
        raise NotFound("초대 코드가 올바르지 않습니다.")

    existing = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )

    if existing is not None:
        if existing.status == MEMBER_STATUS_ACTIVE:
            raise InviteAlreadyJoined()
        # 탈퇴(D18) 후 재참여. UNIQUE(project_id, user_id) 때문에 새 행을 넣을 수 없고,
        # 409 로 막으면 한 번 나간 질문자는 영영 돌아올 수 없다 — 문서에 그런 제한은 없다.
        # 기존 행을 되살리므로 과거 질문·답변·피드백은 그대로 이어진다.
        existing.status = MEMBER_STATUS_ACTIVE
        existing.role = ROLE_ASKER
        member = existing
    else:
        member = ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=ROLE_ASKER,
            status=MEMBER_STATUS_ACTIVE,
        )
        db.add(member)

    await db.flush()

    await record_event(
        db,
        project_id=project.id,
        type=EVENT_MEMBER_JOINED,
        actor_id=user.id,
        entity_type="project_member",
        entity_id=member.id,
        payload={"role": ROLE_ASKER, "via": "invite_code"},
    )
    return to_detail(project, member)


async def regenerate_invite_code(db: AsyncSession, project: Project) -> str:
    project.invite_code = await _generate_unique_invite_code(db)
    await db.flush()
    return project.invite_code


async def patch_settings(db: AsyncSession, project: Project, payload: SettingsPatch) -> Project:
    """부분 갱신. 허용 키·타입·범위는 SettingsPatch 가 이미 강제했다 (`05 §3`).

    JSONB 딕셔너리를 제자리에서 고치면 SQLAlchemy 가 변경을 감지하지 못한다.
    새 딕셔너리를 대입한다.
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return project

    project.settings = {**project.settings, **changes}
    await db.flush()
    return project


async def set_away_mode(db: AsyncSession, project: Project, away_mode: bool) -> Project:
    """퇴근 모드 전환.

    **모드를 꺼도 카드는 사라지지 않는다** (룰 9) — 여기서 지우는 것은 아무것도 없다.
    """
    project.away_mode = away_mode
    await db.flush()
    return project


async def list_members(db: AsyncSession, project_id: UUID) -> list[MemberOut]:
    """탈퇴 멤버(`status='left'`)도 포함한다 — 계약이 "역할·상태" 목록을 요구한다 (`05 §3`)."""
    rows = await db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.joined_at)
    )
    return [
        MemberOut(
            user_id=user.id,
            name=user.name,
            email=user.email,
            role=member.role,
            status=member.status,
            joined_at=member.joined_at,
        )
        for member, user in rows.all()
    ]


async def transfer_answerer(
    db: AsyncSession,
    project: Project,
    actor: User,
    new_answerer_id: UUID,
) -> None:
    """담당자 교체 (D16, `04 §7`).

    ⚠️ **단일 `UPDATE … CASE` 로 스왑하지 않는다.** 부분 UNIQUE 인덱스는 `DEFERRABLE` 을 지원하지
    않아 문장 중간 상태에서 제약을 위반한다. M-1 실측(2026-08-07, 로컬 pg16.14 · 로컬 pg18.4 ·
    Railway pg18.4 동일)에서 단일 UPDATE 는 **행 처리 순서에 따라 통과하기도 했다** —
    항상 터지지 않아서 더 위험하다(테스트는 초록인데 운영에서 간헐 `23505`).
    아래 4문 절차는 순서와 무관하게 항상 성공함이 같은 프로브에서 확인됐다.
    """
    # ① 상위 행 잠금 — 동시 교체 요청을 직렬화한다.
    await db.execute(select(Project.id).where(Project.id == project.id).with_for_update())

    new_member = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == new_answerer_id,
        )
    )
    if new_member is None or new_member.status != MEMBER_STATUS_ACTIVE:
        raise ValidationError("새 담당자는 이 프로젝트의 활성 멤버여야 합니다.")

    previous_answerer_id = project.answerer_id

    # ⚠️ `synchronize_session="fetch"` 는 성능 취향이 아니라 정합성 요구다.
    #    이 요청은 이미 `require_answerer` 가 구담당자의 ProjectMember 를 세션에 올려 둔 상태다.
    #    `False` 로 두면 그 객체가 role='answerer' 인 채로 identity map 에 남고, 같은 세션의
    #    이후 조회(멤버 목록)가 **DB 가 아니라 낡은 객체**를 돌려준다 — 교체가 안 된 것처럼 보인다.
    #    "fetch" 는 갱신된 행의 PK 를 RETURNING 으로 받아 해당 객체의 속성을 만료시킨다.
    #    UPDATE 문 자체와 그 순서는 바뀌지 않으므로 아래 4문 절차의 제약 안전성은 그대로다.

    # ② 구담당자 강등 — 삭제하지 않는다. 질문자로 프로젝트에 남는다 (D16).
    await db.execute(
        update(ProjectMember)
        .where(
            ProjectMember.project_id == project.id,
            ProjectMember.role == ROLE_ANSWERER,
        )
        .values(role=ROLE_ASKER)
        .execution_options(synchronize_session="fetch")
    )

    # ③ 신담당자 승격
    await db.execute(
        update(ProjectMember)
        .where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == new_answerer_id,
        )
        .values(role=ROLE_ANSWERER)
        .execution_options(synchronize_session="fetch")
    )

    # ④ 같은 트랜잭션에서 projects 갱신
    await db.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(answerer_id=new_answerer_id)
        .execution_options(synchronize_session=False)
    )

    # synchronize_session=False 라 세션의 ORM 객체는 낡은 값을 들고 있다.
    # ⚠️ `expire()` 로 두면 다음 속성 접근이 암묵적 IO 를 일으켜 async 에서 MissingGreenlet 이 된다.
    #    여기서 명시적으로 await 해 다시 읽는다.
    await db.refresh(project)
    await db.refresh(new_member)

    # 미처리 카드 이관(기능 6.3, `02 §9`)은 **별도 UPDATE 가 없다.**
    # `review_cards` 는 `project_id` 스코프이고(`04 §2`) 큐 조회 권한은 `require_answerer` 가
    # 판정하므로, ③④ 로 담당자가 바뀌는 순간 pending·deferred 카드가 그대로 신규 담당자의
    # 인박스가 된다. 카드에 담당자 컬럼을 두면 이관 누락이라는 실패 모드가 생길 뿐이다.
    # 브리핑·DND 판정 타임존도 같은 이유로 따라간다 — 단일 원천이 담당자의 users.timezone 이다
    # (`02 §6`).

    await record_event(
        db,
        project_id=project.id,
        type=EVENT_ANSWERER_TRANSFERRED,
        actor_id=actor.id,
        entity_type="project",
        entity_id=project.id,
        payload={
            "from_user_id": str(previous_answerer_id),
            "to_user_id": str(new_answerer_id),
        },
    )


async def leave_project(db: AsyncSession, member: ProjectMember) -> None:
    """질문자 자진 탈퇴 (D18).

    멤버십만 `left` 로 바꾼다 — 기존 질문·답변·피드백은 보존된다.
    담당자는 교체 없이 떠날 수 없으므로(D17) 라우터가 `require_asker` 로 막는다.
    """
    member.status = MEMBER_STATUS_LEFT
    await db.flush()


async def remove_member(db: AsyncSession, project_id: UUID, target_user_id: UUID) -> None:
    """담당자가 멤버를 내보낸다. asker 만 대상이다 (`05 §3`)."""
    target = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == target_user_id,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
    )
    if target is None:
        raise NotFound("해당 멤버를 찾을 수 없습니다.")
    if target.role != ROLE_ASKER:
        # 담당자를 내보내면 담당자 부재 상태가 생긴다 — D17 이 금지한다.
        # 교체는 transfer-answerer 로만 한다.
        raise ForbiddenRole("담당자는 내보낼 수 없습니다. 담당자 교체를 먼저 수행하세요.")

    target.status = MEMBER_STATUS_LEFT
    await db.flush()


async def get_guideline_content(db: AsyncSession, project_id: UUID) -> str | None:
    """저장 전에는 행이 없다 (`04 §1` — 0 또는 1행)."""
    return await db.scalar(select(Guideline.content).where(Guideline.project_id == project_id))


async def upsert_guideline(
    db: AsyncSession, project_id: UUID, content: str, updated_by: UUID
) -> str:
    guideline = await db.get(Guideline, project_id)
    if guideline is None:
        guideline = Guideline(project_id=project_id, content=content, updated_by=updated_by)
        db.add(guideline)
    else:
        guideline.content = content
        guideline.updated_by = updated_by
    await db.flush()
    return guideline.content
