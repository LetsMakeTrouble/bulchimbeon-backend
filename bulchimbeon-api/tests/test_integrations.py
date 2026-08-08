"""외부 연동 등록·조회·해제와 **토큰 보호** (`05 §5`, `03 §7`, `08` 완료 기준).

> ### 이 파일의 핵심 단언은 "평문 토큰이 DB 에도 응답에도 없다"이다
> 나머지 테스트가 전부 통과해도 이것 하나가 깨지면 M8 은 실패다 — 연동은 남의 조직 문서를
> 읽을 수 있는 자격 증명을 우리 DB 에 맡기는 기능이기 때문이다.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import crypto
from app.schemas.integration import DEFAULT_BRANCH, DEFAULT_PATH_GLOB
from tests.helpers import API, create_actor, error_code
from tests.review_helpers import Team, build_team
from tests.sync_helpers import GITHUB_TOKEN, NOTION_TOKEN, register_github, register_notion


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "integrations.test")


async def _stored_config_text(db: AsyncSession, integration_id: str) -> str:
    """jsonb 를 **문자열 그대로** 읽는다 — 어느 키에 들어 있든 평문이면 여기서 걸린다."""
    value = await db.scalar(
        text("SELECT config::text FROM integrations WHERE id = :id"), {"id": integration_id}
    )
    assert value is not None
    return str(value)


# --------------------------------------------------------------------------------------
# 등록 (`05 §5`)
# --------------------------------------------------------------------------------------
async def test_registering_github_masks_the_token_and_fills_defaults(
    client: AsyncClient, team: Team
) -> None:
    body = await register_github(client, team.owner, team.project_id)

    assert body["provider"] == "github"
    # `08 §1` — `ntn_****` 형식. 접두사만 남고 뒤는 길이조차 노출되지 않는다.
    assert body["config"]["token"] == "ghp_****"
    assert GITHUB_TOKEN not in str(body)
    assert body["config"]["repo"] == "acme/partner-docs"
    assert body["last_synced_at"] is None
    assert body["last_sync_status"] is None


async def test_github_config_defaults_are_stored_not_guessed_each_time(
    client: AsyncClient, team: Team
) -> None:
    """`branch`·`path_glob` 을 생략해도 정규형이 저장된다 (`integration_service.create`)."""
    response = await client.post(
        f"{API}/projects/{team.project_id}/integrations",
        json={"provider": "github", "config": {"repo": "acme/docs"}},
        headers=team.owner.headers,
    )

    assert response.status_code == 201, response.text
    config = response.json()["config"]
    assert config["branch"] == DEFAULT_BRANCH
    assert config["path_glob"] == DEFAULT_PATH_GLOB
    # 공개 레포는 토큰 없이 동작한다 (`05 §5`).
    assert config["token"] is None


async def test_registering_notion_masks_the_token(client: AsyncClient, team: Team) -> None:
    body = await register_notion(client, team.owner, team.project_id, ["page-1", "page-2"])

    assert body["config"]["token"] == "ntn_****"
    assert body["config"]["page_ids"] == ["page-1", "page-2"]
    assert NOTION_TOKEN not in str(body)


# --------------------------------------------------------------------------------------
# ⭐ 토큰이 DB 에 평문으로 저장되지 않는다 (`08` 완료 기준, `03 §7`)
# --------------------------------------------------------------------------------------
async def test_the_token_is_never_stored_in_plaintext(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    body = await register_notion(client, team.owner, team.project_id, ["page-1"])

    stored = await _stored_config_text(db_session, body["id"])
    assert NOTION_TOKEN not in stored
    # 마스킹 문자열을 저장하는 것도 아니다 — 저장은 **복호화 가능한 암호문**이어야 한다.
    assert "ntn_****" not in stored
    assert "page-1" in stored  # 비밀이 아닌 필드는 그대로 남는다.


async def test_the_stored_token_round_trips_through_the_encryption_key(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    body = await register_github(client, team.owner, team.project_id)

    stored = await db_session.scalar(
        text("SELECT config->>'token' FROM integrations WHERE id = :id"), {"id": body["id"]}
    )
    assert stored is not None
    assert crypto.decrypt(str(stored)) == GITHUB_TOKEN


async def test_a_changed_encryption_key_is_reported_not_silently_ignored(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키가 바뀌면 조용히 빈 토큰으로 진행하지 않는다 (`core/crypto.py` 독스트링).

    빈 토큰으로 동기화가 돌면 원인이 "GitHub 401" 로 둔갑해 추적이 어려워진다.
    """
    body = await register_github(client, team.owner, team.project_id)
    stored = await db_session.scalar(
        text("SELECT config->>'token' FROM integrations WHERE id = :id"), {"id": body["id"]}
    )

    monkeypatch.setattr(settings, "integration_encryption_key", "완전히-다른-키")
    with pytest.raises(crypto.TokenDecryptionError):
        crypto.decrypt(str(stored))


def test_any_env_string_can_be_used_as_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`03 §4` 의 예시값(`change-me-32bytes`)은 정식 Fernet 키가 아니다 — 그래도 동작해야 한다."""
    monkeypatch.setattr(settings, "integration_encryption_key", "change-me-32bytes")
    assert crypto.decrypt(crypto.encrypt("hello")) == "hello"


def test_masking_hides_everything_after_the_prefix() -> None:
    assert crypto.mask("ntn_abcdefghijklmnop") == "ntn_****"
    assert crypto.mask("ghp_x") == "ghp_****"
    # 접두사가 없는 토큰은 길이조차 노출하지 않는다.
    assert crypto.mask("abcdefghijklmnop") == "****"
    assert crypto.mask("") == ""


# --------------------------------------------------------------------------------------
# 목록·해제 (`05 §5`)
# --------------------------------------------------------------------------------------
async def test_listing_returns_masked_tokens(client: AsyncClient, team: Team) -> None:
    await register_github(client, team.owner, team.project_id)
    await register_notion(client, team.owner, team.project_id, ["page-1"])

    response = await client.get(
        f"{API}/projects/{team.project_id}/integrations", headers=team.owner.headers
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["provider"] for item in items] == ["github", "notion"]
    assert GITHUB_TOKEN not in response.text
    assert NOTION_TOKEN not in response.text


async def test_deleting_an_integration_removes_it_from_the_list(
    client: AsyncClient, team: Team
) -> None:
    body = await register_github(client, team.owner, team.project_id)

    deleted = await client.delete(f"{API}/integrations/{body['id']}", headers=team.owner.headers)
    assert deleted.status_code == 204, deleted.text

    listed = await client.get(
        f"{API}/projects/{team.project_id}/integrations", headers=team.owner.headers
    )
    assert listed.json()["items"] == []


# --------------------------------------------------------------------------------------
# 권한 — 전 경로 담당자 전용 (`05 §5`, 룰 5)
# --------------------------------------------------------------------------------------
async def test_askers_cannot_touch_integrations(client: AsyncClient, team: Team) -> None:
    body = await register_github(client, team.owner, team.project_id)

    created = await client.post(
        f"{API}/projects/{team.project_id}/integrations",
        json={"provider": "github", "config": {"repo": "acme/docs"}},
        headers=team.asker.headers,
    )
    listed = await client.get(
        f"{API}/projects/{team.project_id}/integrations", headers=team.asker.headers
    )
    synced = await client.post(f"{API}/integrations/{body['id']}/sync", headers=team.asker.headers)
    deleted = await client.delete(f"{API}/integrations/{body['id']}", headers=team.asker.headers)

    for response in (created, listed, synced, deleted):
        assert response.status_code == 403, response.text
        assert error_code(response) == "FORBIDDEN_ROLE"


async def test_non_members_get_404_so_the_integration_id_never_leaks(
    client: AsyncClient, team: Team
) -> None:
    body = await register_github(client, team.owner, team.project_id)
    outsider = await create_actor(client, "outsider@integrations.test")

    response = await client.post(f"{API}/integrations/{body['id']}/sync", headers=outsider.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# config 검증 — 계약서 §1.4 안의 코드만 쓴다 (400 `VALIDATION_ERROR`)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("provider", "config"),
    [
        ("notion", {"token": "ntn_x"}),  # page_ids 누락
        ("notion", {"token": "ntn_x", "page_ids": []}),  # 빈 목록
        ("notion", {"page_ids": ["p"]}),  # 토큰 누락 — Notion 은 공개 페이지도 토큰이 필요하다
        ("github", {"repo": "no-slash"}),  # owner/name 형식 아님
        ("github", {}),  # repo 누락
        ("github", {"repo": "a/b", "page_ids": ["p"]}),  # 다른 provider 의 키
        ("dropbox", {"repo": "a/b"}),  # 계약에 없는 provider
    ],
)
async def test_invalid_configs_are_rejected_with_400(
    client: AsyncClient, team: Team, provider: str, config: dict
) -> None:
    response = await client.post(
        f"{API}/projects/{team.project_id}/integrations",
        json={"provider": provider, "config": config},
        headers=team.owner.headers,
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"
