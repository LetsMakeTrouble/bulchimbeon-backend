"""인증 (`05 §2`) — 가입·로그인·refresh·me, 토큰 만료."""

from datetime import timedelta
from uuid import UUID

from httpx import AsyncClient

from app.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from tests.helpers import API, PASSWORD, create_actor, create_project, error_code


async def test_signup_returns_user_without_secrets(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/signup",
        json={
            "email": "jisoo@example.com",
            "password": PASSWORD,
            "name": "지수",
            "language": "ko",
            "timezone": "Asia/Seoul",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "jisoo@example.com"
    assert body["language"] == "ko"
    assert body["timezone"] == "Asia/Seoul"
    # 해시가 응답에 새면 오프라인 크래킹 대상이 된다.
    assert "password" not in body
    assert "password_hash" not in body


async def test_signup_rejects_duplicate_email(client: AsyncClient) -> None:
    await create_actor(client, "dup@example.com")

    response = await client.post(
        f"{API}/auth/signup",
        json={"email": "dup@example.com", "password": PASSWORD, "name": "다른 사람"},
    )

    # 계약서 409 목록에 이메일 중복용 코드가 없다 — 새 코드를 만들지 않고 400 으로 떨어뜨린다.
    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


async def test_signup_rejects_invalid_timezone(client: AsyncClient) -> None:
    """브리핑·DND 판정이 이 값을 그대로 쓴다 (`02 §6`) — 여기서 막지 않으면 M6 에서 터진다."""
    response = await client.post(
        f"{API}/auth/signup",
        json={
            "email": "badtz@example.com",
            "password": PASSWORD,
            "name": "타임존",
            "timezone": "Mars/Olympus",
        },
    )

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


async def test_login_with_wrong_password_is_unauthorized(client: AsyncClient) -> None:
    await create_actor(client, "login@example.com")

    response = await client.post(
        f"{API}/auth/login", json={"email": "login@example.com", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert error_code(response) == "UNAUTHORIZED"


async def test_login_with_unknown_email_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    # 존재 여부를 응답으로 흘리지 않는다 — 비밀번호 오류와 같은 401 이다.
    assert response.status_code == 401
    assert error_code(response) == "UNAUTHORIZED"


async def test_refresh_issues_new_token_pair(client: AsyncClient) -> None:
    actor = await create_actor(client, "refresh@example.com")

    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["id"] == actor.id


async def test_expired_access_token_returns_token_expired(client: AsyncClient) -> None:
    """DoD — 만료 토큰은 401 `TOKEN_EXPIRED` 다 (`05 §1.4`)."""
    actor = await create_actor(client, "expired@example.com")
    expired = create_access_token(UUID(actor.id), expires_delta=timedelta(minutes=-1))

    response = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401
    assert error_code(response) == "TOKEN_EXPIRED"


async def test_refresh_token_is_rejected_as_access_token(client: AsyncClient) -> None:
    """`type` 클레임이 없으면 14일짜리 refresh 로 30분 만료를 우회할 수 있다."""
    actor = await create_actor(client, "typemix@example.com")

    response = await client.get(
        f"{API}/auth/me", headers={"Authorization": f"Bearer {actor.refresh_token}"}
    )

    assert response.status_code == 401
    assert error_code(response) == "UNAUTHORIZED"


async def test_expired_refresh_token_returns_token_expired(client: AsyncClient) -> None:
    actor = await create_actor(client, "expiredrefresh@example.com")
    expired = create_refresh_token(UUID(actor.id), expires_delta=timedelta(days=-1))

    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": expired})

    assert response.status_code == 401
    assert error_code(response) == "TOKEN_EXPIRED"


async def test_missing_and_garbage_tokens_are_unauthorized(client: AsyncClient) -> None:
    no_header = await client.get(f"{API}/auth/me")
    assert no_header.status_code == 401
    assert error_code(no_header) == "UNAUTHORIZED"

    garbage = await client.get(f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert garbage.status_code == 401
    assert error_code(garbage) == "UNAUTHORIZED"


async def test_me_lists_active_projects_with_roles(client: AsyncClient) -> None:
    owner = await create_actor(client, "owner-me@example.com")
    project = await create_project(client, owner)

    response = await client.get(f"{API}/auth/me", headers=owner.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == owner.id
    assert len(body["projects"]) == 1

    summary = body["projects"][0]
    assert summary["id"] == project["id"]
    assert summary["role"] == "answerer"
    assert summary["member_status"] == "active"
    assert summary["away_mode"] is False
    # 원천 테이블은 M4(review_cards)·M5(notifications)에서 생긴다. 계약상 필드는 지키고 0 이다.
    assert summary["unread_notifications"] == 0
    assert summary["pending_cards"] == 0
    assert body["unread_notifications_total"] == 0


def test_password_hash_is_argon2_and_verifies() -> None:
    hashed = hash_password(PASSWORD)

    assert hashed != PASSWORD
    assert hashed.startswith("$argon2")
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("other-password", hashed)


def test_verify_password_returns_false_on_corrupt_hash() -> None:
    """손상된 해시가 예외로 새면 로그인 실패가 500 이 된다."""
    assert not verify_password(PASSWORD, "not-a-hash")


def test_token_types_are_distinct() -> None:
    assert ACCESS_TOKEN_TYPE != REFRESH_TOKEN_TYPE
