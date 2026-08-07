"""전역 예외 핸들러 — 에러 포맷과 유출 방어 (`05 §1.4`, `05 §1.5`, `03 §7`)."""

from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, field_validator

from app.core.errors import AlreadyResolved, _error_object, error_payload
from app.main import app


class _SignupLike(BaseModel):
    """M1 `POST /auth/signup` 과 같은 모양 — 비밀번호를 받는 스키마."""

    email: str
    password: str
    name: str

    @field_validator("email")
    @classmethod
    def _must_look_like_email(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("이메일 형식이 아닙니다")
        return value


@pytest.fixture
async def probe_client() -> AsyncIterator[AsyncClient]:
    """검증 스키마를 가진 임시 라우트를 붙인 클라이언트."""
    router = APIRouter()

    @router.post("/__probe/signup")
    async def _probe(body: _SignupLike) -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/__probe/signup"
        ]


async def test_validation_error_never_echoes_submitted_values(
    probe_client: AsyncClient,
) -> None:
    """제출된 원본 값(=평문 비밀번호)이 응답에 실리면 안 된다.

    pydantic 의 `errors()` 는 항목마다 `input` 에 제출값을 담는다. 그대로 응답에 실으면
    필드 하나만 빠져도 비밀번호가 응답 본문·프록시 로그·프론트 콘솔에 남는다 (`03 §7`).
    """
    response = await probe_client.post(
        "/__probe/signup", json={"email": "a@b.c", "password": "SuperSecret!23"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "요청 형식이 올바르지 않습니다."}
    }
    assert "SuperSecret!23" not in response.text


async def test_custom_validator_error_still_returns_contract_format(
    probe_client: AsyncClient,
) -> None:
    """`field_validator` 가 ValueError 를 던지면 `ctx` 에 예외 객체가 들어온다.

    이걸 응답에 실으면 직렬화가 실패해 계약된 400 대신 500 이 나간다.
    """
    response = await probe_client.post(
        "/__probe/signup", json={"email": "nope", "password": "p", "name": "n"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unknown_route_returns_korean_contract_error(client: AsyncClient) -> None:
    """`05 §1.5` — error.message 는 개발자용 한국어 고정. starlette 영문 detail 을 흘리지 않는다."""
    response = await client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "대상을 찾을 수 없습니다."}
    }


def test_extra_fields_cannot_override_contract_keys() -> None:
    """`ALREADY_RESOLVED` 처럼 추가 필드를 실어도 code·message 는 계약값을 유지한다 (`05 §1.4`)."""
    payload = AlreadyResolved(resolution="approved", code="SPOOFED").to_payload()

    assert payload["error"]["code"] == "ALREADY_RESOLVED"
    assert payload["error"]["resolution"] == "approved"


def test_error_payload_helper_merges_extra_without_losing_contract_keys() -> None:
    payload = error_payload("NOT_FOUND", "대상을 찾을 수 없습니다.", resolved_by="u-2")

    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["error"]["message"] == "대상을 찾을 수 없습니다."
    assert payload["error"]["resolved_by"] == "u-2"


def test_error_object_guard_rejects_contract_key_override() -> None:
    """extra 로 code·message 를 덮어쓰려 해도 계약값이 이긴다."""
    error = _error_object("NOT_FOUND", "대상을 찾을 수 없습니다.", {"code": "SPOOFED", "x": 1})

    assert error["code"] == "NOT_FOUND"
    assert error["x"] == 1
