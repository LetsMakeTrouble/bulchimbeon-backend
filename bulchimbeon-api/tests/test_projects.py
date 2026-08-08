"""프로젝트·멤버·지침 (`05 §3`, `02 §9`) — 초대 플로우·역할 강제·설정·담당자 교체·탈퇴."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.event import Event
from app.models.project import Project, ProjectMember
from app.schemas.project import SettingsPatch
from app.services.event_service import EVENT_ANSWERER_TRANSFERRED, EVENT_MEMBER_JOINED
from tests.helpers import API, create_actor, create_project, error_code, join_project

# ---------------------------------------------------------------------------------------
# DoD — 가입 → 로그인 → 프로젝트 생성 → 초대 코드 참여 → 역할 확인 e2e
# ---------------------------------------------------------------------------------------


async def test_invite_flow_end_to_end(client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await create_actor(client, "e2e-owner@example.com", name="담당")
    project = await create_project(client, owner)

    # 생성자 = 담당자 (D1), settings 는 DEFAULT_SETTINGS 복사, invite_code 자동 발급
    assert project["role"] == "answerer"
    assert project["settings"] == DEFAULT_SETTINGS
    assert project["invite_code"]

    asker = await create_actor(client, "e2e-asker@example.com", name="질문")
    joined = await join_project(client, asker, project["invite_code"])
    assert joined["id"] == project["id"]
    assert joined["role"] == "asker"

    members = await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    assert members.status_code == 200
    roles = {m["user_id"]: m["role"] for m in members.json()["items"]}
    assert roles == {owner.id: "answerer", asker.id: "asker"}

    # 룰 4 — 참여는 상태 변화이므로 events 에 남는다.
    joined_events = await db_session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.project_id == project["id"], Event.type == EVENT_MEMBER_JOINED)
    )
    assert joined_events == 2  # 생성자 1 + 초대 참여 1


async def test_settings_copy_is_not_shared_between_projects(client: AsyncClient) -> None:
    """DEFAULT_SETTINGS 를 참조로 넣으면 한 프로젝트의 PATCH 가 전역 기본값을 오염시킨다."""
    owner = await create_actor(client, "copy@example.com")
    first = await create_project(client, owner, name="첫 프로젝트")
    second = await create_project(client, owner, name="둘째 프로젝트")

    await client.patch(
        f"{API}/projects/{first['id']}/settings",
        json={"green_threshold": 95},
        headers=owner.headers,
    )

    detail = await client.get(f"{API}/projects/{second['id']}", headers=owner.headers)
    assert detail.json()["settings"]["green_threshold"] == DEFAULT_SETTINGS["green_threshold"]
    assert DEFAULT_SETTINGS["green_threshold"] == 80


async def test_join_twice_returns_invite_already_joined(client: AsyncClient) -> None:
    owner = await create_actor(client, "dupjoin-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "dupjoin-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    response = await client.post(
        f"{API}/projects/join",
        json={"invite_code": project["invite_code"]},
        headers=asker.headers,
    )

    assert response.status_code == 409
    assert error_code(response) == "INVITE_ALREADY_JOINED"


async def test_join_with_unknown_invite_code_is_not_found(client: AsyncClient) -> None:
    actor = await create_actor(client, "badcode@example.com")

    response = await client.post(
        f"{API}/projects/join", json={"invite_code": "NOSUCHCODE"}, headers=actor.headers
    )

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"


async def test_invite_code_regeneration_invalidates_old_code(client: AsyncClient) -> None:
    owner = await create_actor(client, "regen-owner@example.com")
    project = await create_project(client, owner)
    old_code = project["invite_code"]

    regenerated = await client.post(
        f"{API}/projects/{project['id']}/invite-code", headers=owner.headers
    )
    assert regenerated.status_code == 200
    new_code = regenerated.json()["invite_code"]
    assert new_code != old_code

    latecomer = await create_actor(client, "regen-late@example.com")
    stale = await client.post(
        f"{API}/projects/join", json={"invite_code": old_code}, headers=latecomer.headers
    )
    assert stale.status_code == 404

    fresh = await client.post(
        f"{API}/projects/join", json={"invite_code": new_code}, headers=latecomer.headers
    )
    assert fresh.status_code == 200


async def test_invite_code_is_visible_only_to_answerer(client: AsyncClient) -> None:
    """초대 권한은 담당자에게만 있다 (기능 6.2)."""
    owner = await create_actor(client, "code-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "code-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    as_answerer = await client.get(f"{API}/projects/{project['id']}", headers=owner.headers)
    assert as_answerer.json()["invite_code"] == project["invite_code"]

    as_asker = await client.get(f"{API}/projects/{project['id']}", headers=asker.headers)
    assert as_asker.status_code == 200
    assert as_asker.json()["invite_code"] is None


# ---------------------------------------------------------------------------------------
# DoD — 권한 (룰 5: 권한은 서버가 강제)
# ---------------------------------------------------------------------------------------


async def test_asker_cannot_patch_settings(client: AsyncClient) -> None:
    owner = await create_actor(client, "perm-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "perm-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    response = await client.patch(
        f"{API}/projects/{project['id']}/settings",
        json={"green_threshold": 90},
        headers=asker.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_non_member_is_forbidden(client: AsyncClient) -> None:
    owner = await create_actor(client, "closed-owner@example.com")
    project = await create_project(client, owner)
    outsider = await create_actor(client, "outsider@example.com")

    detail = await client.get(f"{API}/projects/{project['id']}", headers=outsider.headers)
    assert detail.status_code == 403
    assert error_code(detail) == "NOT_MEMBER"

    members = await client.get(f"{API}/projects/{project['id']}/members", headers=outsider.headers)
    assert members.status_code == 403
    assert error_code(members) == "NOT_MEMBER"


async def test_unknown_project_is_not_found(client: AsyncClient) -> None:
    actor = await create_actor(client, "ghost@example.com")

    response = await client.get(
        f"{API}/projects/00000000-0000-0000-0000-000000000000", headers=actor.headers
    )

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "/away-mode", {"away_mode": True}),
        ("post", "/invite-code", None),
        ("put", "/guidelines", {"content": "지침"}),
    ],
)
async def test_answerer_only_endpoints_reject_asker(
    client: AsyncClient, method: str, suffix: str, body: dict | None
) -> None:
    owner = await create_actor(client, f"only-owner{suffix.replace('/', '-')}@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, f"only-asker{suffix.replace('/', '-')}@example.com")
    await join_project(client, asker, project["invite_code"])

    request = getattr(client, method)
    kwargs = {"headers": asker.headers}
    if body is not None:
        kwargs["json"] = body
    response = await request(f"{API}/projects/{project['id']}{suffix}", **kwargs)

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_guidelines_are_readable_by_members(client: AsyncClient) -> None:
    owner = await create_actor(client, "guide-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "guide-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    # `04 §1` — 신규 프로젝트에는 guidelines 행이 없다.
    empty = await client.get(f"{API}/projects/{project['id']}/guidelines", headers=asker.headers)
    assert empty.status_code == 200
    assert empty.json()["content"] is None

    saved = await client.put(
        f"{API}/projects/{project['id']}/guidelines",
        json={"content": "숫자는 반드시 문서에서 인용한다."},
        headers=owner.headers,
    )
    assert saved.status_code == 200

    # 두 번째 PUT 은 upsert 다 — 행이 늘지 않고 갱신된다.
    updated = await client.put(
        f"{API}/projects/{project['id']}/guidelines",
        json={"content": "갱신된 지침"},
        headers=owner.headers,
    )
    assert updated.json()["content"] == "갱신된 지침"

    read_back = await client.get(
        f"{API}/projects/{project['id']}/guidelines", headers=asker.headers
    )
    assert read_back.json()["content"] == "갱신된 지침"


async def test_away_mode_toggles(client: AsyncClient) -> None:
    owner = await create_actor(client, "away@example.com")
    project = await create_project(client, owner)

    on = await client.patch(
        f"{API}/projects/{project['id']}/away-mode",
        json={"away_mode": True},
        headers=owner.headers,
    )
    assert on.status_code == 200
    assert on.json()["away_mode"] is True

    off = await client.patch(
        f"{API}/projects/{project['id']}/away-mode",
        json={"away_mode": False},
        headers=owner.headers,
    )
    assert off.json()["away_mode"] is False


# ---------------------------------------------------------------------------------------
# DoD — settings PATCH 화이트리스트 (`05 §3` 허용 키 16개)
# ---------------------------------------------------------------------------------------

# 계약서 `05 §3` 표의 타입·범위 안에서 기본값과 **다른** 값을 고른다.
SETTINGS_SAMPLE: dict[str, object] = {
    "green_threshold": 85,
    "yellow_threshold": 45,
    "grounding_min": 70,
    "s_floor": 0.3,
    "s_ceil": 0.7,
    "similarity_floor": 0.45,
    "reuse_threshold": 0.93,
    "similar_threshold": 0.86,
    "draft_expire_hours": 48,
    "max_lessons": 20,
    "retrieval_top_k": 8,
    "daily_llm_call_limit": 300,
    "saved_wait_assumption_hours": 12,
    "briefing_hour": 8,
    "dnd_start": "23:00",
    "dnd_end": "06:30",
}


def test_settings_sample_covers_every_allowed_key() -> None:
    """샘플이 16개를 다 덮지 않으면 아래 파라미터 테스트가 조용히 구멍을 남긴다."""
    assert set(SETTINGS_SAMPLE) == set(DEFAULT_SETTINGS)
    assert len(SETTINGS_SAMPLE) == 16
    assert set(SettingsPatch.model_fields) == set(DEFAULT_SETTINGS)
    # `04 §3`·`05 §3` — 이 키는 존재하지 않는다.
    assert "briefing_timezone" not in DEFAULT_SETTINGS


@pytest.mark.parametrize("key", sorted(SETTINGS_SAMPLE))
async def test_each_allowed_settings_key_updates(client: AsyncClient, key: str) -> None:
    owner = await create_actor(client, f"set-{key.replace('_', '-')}@example.com")
    project = await create_project(client, owner)
    value = SETTINGS_SAMPLE[key]

    response = await client.patch(
        f"{API}/projects/{project['id']}/settings", json={key: value}, headers=owner.headers
    )

    assert response.status_code == 200, response.text
    settings = response.json()["settings"]
    assert settings[key] == value
    # 부분 갱신이다 — 나머지 키는 그대로여야 한다.
    for other, default in DEFAULT_SETTINGS.items():
        if other != key:
            assert settings[other] == default


async def test_settings_patch_rejects_briefing_timezone(client: AsyncClient) -> None:
    """DoD — `briefing_timezone` 은 허용 키가 아니다.

    브리핑·DND 시각 판정의 단일 원천은 현재 담당자의 `users.timezone` 이며
    프로젝트 설정으로 덮어쓸 수 없다 (`04 §3`, `05 §3`).
    """
    owner = await create_actor(client, "tzkey@example.com")
    project = await create_project(client, owner)

    response = await client.patch(
        f"{API}/projects/{project['id']}/settings",
        json={"green_threshold": 85, "briefing_timezone": "Asia/Seoul"},
        headers=owner.headers,
    )

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"

    # 거부된 요청이 같이 실린 허용 키까지 반영해서는 안 된다.
    detail = await client.get(f"{API}/projects/{project['id']}", headers=owner.headers)
    assert detail.json()["settings"]["green_threshold"] == DEFAULT_SETTINGS["green_threshold"]


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown_key": 1},
        {"green_threshold": 101},
        {"green_threshold": -1},
        {"s_floor": 1.5},
        {"briefing_hour": 24},
        {"dnd_start": "25:00"},
        {"dnd_end": "7:00"},
        {"green_threshold": "high"},
        {"green_threshold": None},
        {"retrieval_top_k": 0},
    ],
)
async def test_settings_patch_rejects_out_of_contract_values(
    client: AsyncClient, payload: dict
) -> None:
    owner = await create_actor(client, f"bad-{abs(hash(str(payload)))}@example.com")
    project = await create_project(client, owner)

    response = await client.patch(
        f"{API}/projects/{project['id']}/settings", json=payload, headers=owner.headers
    )

    assert response.status_code == 400, f"{payload} -> {response.status_code}"
    assert error_code(response) == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------------------
# DoD — 담당자 교체 (D16, `04 §7`)
# ---------------------------------------------------------------------------------------


async def _active_answerer_ids(db: AsyncSession, project_id: str) -> list[str]:
    rows = await db.execute(
        select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "answerer",
            ProjectMember.status == "active",
        )
    )
    return [str(row) for row in rows.scalars().all()]


async def test_transfer_answerer_swaps_roles(client: AsyncClient, db_session: AsyncSession) -> None:
    """DoD — 교체 후 담당자 정확히 1명, 구담당자는 asker 로 남고, projects.answerer_id 갱신."""
    owner = await create_actor(client, "transfer-owner@example.com")
    project = await create_project(client, owner)
    successor = await create_actor(client, "transfer-next@example.com")
    await join_project(client, successor, project["invite_code"])

    response = await client.post(
        f"{API}/projects/{project['id']}/transfer-answerer",
        json={"new_answerer_id": successor.id},
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text

    roles = {m["user_id"]: m["role"] for m in response.json()["items"]}
    assert roles[successor.id] == "answerer"
    # 구담당자는 삭제되지 않고 asker 로 프로젝트에 남는다 (D16).
    assert roles[owner.id] == "asker"

    assert await _active_answerer_ids(db_session, project["id"]) == [successor.id]

    stored = await db_session.get(Project, project["id"])
    await db_session.refresh(stored)
    assert str(stored.answerer_id) == successor.id

    transferred = await db_session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.project_id == project["id"], Event.type == EVENT_ANSWERER_TRANSFERRED)
    )
    assert transferred == 1


async def test_transfer_answerer_survives_both_swap_directions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M-1 실측 회귀 — 단일 UPDATE 스왑은 **행 처리 순서에 따라** 통과하기도 한다 (`04 §7`).

    한 방향만 시험하면 초록으로 통과한 뒤 운영에서 간헐 `23505` 가 난다.
    앞뒤로 교체해 어느 순서가 불리하든 반드시 밟히게 한다.
    """
    owner = await create_actor(client, "pingpong-a@example.com")
    project = await create_project(client, owner)
    other = await create_actor(client, "pingpong-b@example.com")
    third = await create_actor(client, "pingpong-c@example.com")
    await join_project(client, other, project["invite_code"])
    await join_project(client, third, project["invite_code"])

    holder, challenger = owner, other
    for _ in range(4):
        response = await client.post(
            f"{API}/projects/{project['id']}/transfer-answerer",
            json={"new_answerer_id": challenger.id},
            headers=holder.headers,
        )
        assert response.status_code == 200, response.text
        assert await _active_answerer_ids(db_session, project["id"]) == [challenger.id]
        holder, challenger = challenger, holder


async def test_transfer_answerer_rejects_non_member_and_left_member(
    client: AsyncClient,
) -> None:
    owner = await create_actor(client, "reject-owner@example.com")
    project = await create_project(client, owner)
    outsider = await create_actor(client, "reject-outsider@example.com")
    leaver = await create_actor(client, "reject-leaver@example.com")
    await join_project(client, leaver, project["invite_code"])
    await client.post(f"{API}/projects/{project['id']}/leave", headers=leaver.headers)

    for target in (outsider, leaver):
        response = await client.post(
            f"{API}/projects/{project['id']}/transfer-answerer",
            json={"new_answerer_id": target.id},
            headers=owner.headers,
        )
        assert response.status_code == 400, response.text
        assert error_code(response) == "VALIDATION_ERROR"


async def test_asker_cannot_transfer_answerer(client: AsyncClient) -> None:
    owner = await create_actor(client, "notransfer-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "notransfer-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    response = await client.post(
        f"{API}/projects/{project['id']}/transfer-answerer",
        json={"new_answerer_id": asker.id},
        headers=asker.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_transfer_moves_briefing_timezone_source(client: AsyncClient) -> None:
    """타임존은 별도 코드 없이 신담당자를 따라간다.

    단일 원천이 users.timezone 이기 때문이다 (`02 §6`).
    """
    owner = await create_actor(client, "tz-owner@example.com", timezone="Asia/Seoul")
    project = await create_project(client, owner)
    successor = await create_actor(client, "tz-next@example.com", timezone="America/New_York")
    await join_project(client, successor, project["invite_code"])

    await client.post(
        f"{API}/projects/{project['id']}/transfer-answerer",
        json={"new_answerer_id": successor.id},
        headers=owner.headers,
    )

    detail = await client.get(f"{API}/projects/{project['id']}", headers=successor.headers)
    assert detail.json()["role"] == "answerer"
    # settings 에는 타임존 키가 없다 — 판정은 담당자 users.timezone 을 본다.
    assert "briefing_timezone" not in detail.json()["settings"]


# ---------------------------------------------------------------------------------------
# DoD — 탈퇴 (D17 · D18)
# ---------------------------------------------------------------------------------------


async def test_asker_can_leave_and_membership_row_is_preserved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """DoD — asker 탈퇴 성공 + 기존 데이터 보존 (D18)."""
    owner = await create_actor(client, "leave-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "leave-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    response = await client.post(f"{API}/projects/{project['id']}/leave", headers=asker.headers)
    assert response.status_code == 200
    assert response.json()["member_status"] == "left"

    # 행은 남고 상태만 바뀐다 — 질문·답변·피드백의 FK 가 살아 있어야 한다.
    member = await db_session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project["id"],
            ProjectMember.user_id == asker.id,
        )
    )
    assert member is not None
    await db_session.refresh(member)
    assert member.status == "left"

    # 탈퇴한 프로젝트는 목록에서 빠진다 (`05 §2`).
    listed = await client.get(f"{API}/projects", headers=asker.headers)
    assert listed.json()["items"] == []

    me = await client.get(f"{API}/auth/me", headers=asker.headers)
    assert me.json()["projects"] == []

    # 담당자 화면의 멤버 목록에는 상태와 함께 남는다.
    members = await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    left_rows = [m for m in members.json()["items"] if m["user_id"] == asker.id]
    assert left_rows and left_rows[0]["status"] == "left"

    # 접근은 비멤버와 같다.
    blocked = await client.get(f"{API}/projects/{project['id']}", headers=asker.headers)
    assert blocked.status_code == 403
    assert error_code(blocked) == "NOT_MEMBER"


async def test_answerer_cannot_leave(client: AsyncClient) -> None:
    """DoD — 담당자 탈퇴 시도는 403 `FORBIDDEN_ROLE` (D17).

    담당자는 교체 없이 떠날 수 없으므로 담당자 부재 상태가 생기지 않는다.
    """
    owner = await create_actor(client, "noleave@example.com")
    project = await create_project(client, owner)

    response = await client.post(f"{API}/projects/{project['id']}/leave", headers=owner.headers)

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_left_member_can_rejoin_with_invite_code(client: AsyncClient) -> None:
    """UNIQUE(project_id, user_id) 때문에 새 행을 넣을 수 없다 — 기존 행을 되살린다."""
    owner = await create_actor(client, "rejoin-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "rejoin-asker@example.com")
    await join_project(client, asker, project["invite_code"])
    await client.post(f"{API}/projects/{project['id']}/leave", headers=asker.headers)

    rejoined = await client.post(
        f"{API}/projects/join",
        json={"invite_code": project["invite_code"]},
        headers=asker.headers,
    )

    assert rejoined.status_code == 200
    assert rejoined.json()["role"] == "asker"
    assert rejoined.json()["member_status"] == "active"


async def test_answerer_removes_asker_but_not_answerer(client: AsyncClient) -> None:
    owner = await create_actor(client, "kick-owner@example.com")
    project = await create_project(client, owner)
    asker = await create_actor(client, "kick-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    removed = await client.delete(
        f"{API}/projects/{project['id']}/members/{asker.id}", headers=owner.headers
    )
    assert removed.status_code == 204

    blocked = await client.get(f"{API}/projects/{project['id']}", headers=asker.headers)
    assert blocked.status_code == 403

    # 담당자를 내보내면 담당자 부재가 생긴다 — D17 이 금지한다.
    self_kick = await client.delete(
        f"{API}/projects/{project['id']}/members/{owner.id}", headers=owner.headers
    )
    assert self_kick.status_code == 403
    assert error_code(self_kick) == "FORBIDDEN_ROLE"


async def test_removing_unknown_member_is_not_found(client: AsyncClient) -> None:
    owner = await create_actor(client, "kick-ghost@example.com")
    project = await create_project(client, owner)
    outsider = await create_actor(client, "kick-outsider@example.com")

    response = await client.delete(
        f"{API}/projects/{project['id']}/members/{outsider.id}", headers=owner.headers
    )

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"
