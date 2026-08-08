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
