"""테스트 공통 헬퍼 — 계약서 경로(`/api/v1`)를 한 곳에서만 적는다."""

from typing import Any

from httpx import AsyncClient

API = "/api/v1"
PASSWORD = "test-password-1234"


class Actor:
    """가입·로그인까지 끝낸 테스트 유저."""

    def __init__(self, user: dict[str, Any], access_token: str, refresh_token: str) -> None:
        self.user = user
        self.access_token = access_token
        self.refresh_token = refresh_token

    @property
    def id(self) -> str:
        return self.user["id"]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def create_actor(
    client: AsyncClient,
    email: str,
    *,
    name: str = "테스터",
    language: str = "ko",
    timezone: str = "Asia/Seoul",
) -> Actor:
    signup = await client.post(
        f"{API}/auth/signup",
        json={
            "email": email,
            "password": PASSWORD,
            "name": name,
            "language": language,
            "timezone": timezone,
        },
    )
    assert signup.status_code == 201, signup.text

    login = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    return Actor(body["user"], body["access_token"], body["refresh_token"])


async def create_project(client: AsyncClient, owner: Actor, name: str = "Acme 파트너십") -> dict:
    response = await client.post(
        f"{API}/projects",
        json={"name": name, "description": "테스트 프로젝트"},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def close_dnd_window(client: AsyncClient, owner: Actor, project_id: str) -> None:
    """이 프로젝트의 DND 판정을 **항상 꺼 둔다** (`dnd.in_dnd_window` 은 start == end 면 False).

    ⚠️ **시각 의존 테스트를 없애기 위한 것이다** (M9 실측 2026-08-09 04:14 UTC).
    기본값 `22:00~07:00` 을 그대로 두면 담당자 타임존(테스트는 UTC) 기준 밤에 도는 실행에서만
    (a) `low_confidence` 🔴 이 🟡 로 강등되고 (룰 6 · D2) (b) 알림이 `deliver_after` 로 보류돼
    알림함에서 사라진다 (`05 §11`). 그래서 **하루의 9시간 동안만 깨지는** 테스트가 된다 —
    실제로 M8 커밋에서 `test_pipeline` 2건과 `test_sync_github` 1건이 그렇게 실패했다.
    `notification_helpers` 가 M5 에서 같은 함정을 창을 직접 계산해 피한 것과 같은 조치다.

    DND 동작 자체를 보는 테스트는 이 뒤에 자기 창을 명시적으로 덮어쓴다 (부분 갱신이므로
    나중 값이 이긴다).
    """
    response = await client.patch(
        f"{API}/projects/{project_id}/settings",
        json={"dnd_start": "00:00", "dnd_end": "00:00"},
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text


async def join_project(client: AsyncClient, actor: Actor, invite_code: str) -> dict:
    response = await client.post(
        f"{API}/projects/join",
        json={"invite_code": invite_code},
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def error_code(response: Any) -> str:
    """`05 §1.4` — 에러는 항상 `{"error": {"code", "message"}}` 다."""
    return response.json()["error"]["code"]


def error_message(response: Any) -> str:
    """`error.message` 는 **개발자용 한국어 고정**이다 (`05 §1.5`).

    같은 `code` 를 여러 관문이 낼 때(예: 400 `VALIDATION_ERROR`) 어느 쪽이 잡았는지를
    테스트가 구분하는 유일한 수단이다.
    """
    return response.json()["error"]["message"]
