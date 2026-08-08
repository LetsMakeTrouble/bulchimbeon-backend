"""M8 동기화 테스트 헬퍼 — 원격만 가짜로 만들고 **나머지는 전부 진짜 경로**를 태운다.

GitHub 은 `httpx.MockTransport` 로 응답만 갈아끼운다. 그래서 헤더·URL·`path_glob` 매칭·
sha 판정·에러 매핑이 전부 실제 코드로 실행된다 — 클라이언트를 통째로 목킹하면 그 부분이
테스트에서 빠져 운영에서만 틀린다.

Notion 은 `notion-client` 의 엔드포인트 객체를 흉내 낸 스텁을 넣는다 (`08` 완료 기준의
"notion-client 목킹"). HTTP 계층이 SDK 안에 있어 transport 만 갈아끼울 수 없기 때문이다.
"""

from typing import Any
from urllib.parse import unquote

import httpx
from httpx import AsyncClient
from notion_client.errors import APIResponseError

from app.services.sync import runner
from app.services.sync.base import git_blob_sha
from app.services.sync.github_sync import GithubSync
from app.services.sync.notion_sync import NotionSync
from tests.helpers import API, Actor

REPO = "acme/partner-docs"
BRANCH = "main"
PATH_GLOB = "docs/**/*.md"

GITHUB_TOKEN = "ghp_secret_github_token_value"
NOTION_TOKEN = "ntn_secret_notion_token_value"

REFUND_MD = b"""# Refund Policy

Refunds are accepted within 30 days of purchase.

## Partial Refunds

Partial refunds follow the same window.
"""

REFUND_MD_V2 = b"""# Refund Policy

Refunds in Japan follow a 20-day window.

## Partial Refunds

Partial refunds follow the same window.
"""


# --------------------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------------------
def github_transport(
    files: dict[str, bytes],
    *,
    truncated: bool = False,
    tree_status: int = 200,
    content_status: dict[str, int] | None = None,
) -> httpx.MockTransport:
    """트리 API 와 contents API 를 흉내 낸다. `files` 는 **호출 시점에 읽히는 참조**다 —
    테스트가 딕셔너리를 고치면 다음 동기화가 그 내용을 본다."""
    failures = content_status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)

        if "/git/trees/" in path:
            if tree_status != 200:
                return httpx.Response(tree_status, json={"message": "boom"})
            tree = [
                {"path": name, "type": "blob", "mode": "100644", "sha": git_blob_sha(content)}
                for name, content in files.items()
            ]
            # 디렉터리 항목도 섞여 오는 것이 실제 응답이다 — 걸러지는지 함께 본다.
            tree.append({"path": "docs", "type": "tree", "mode": "040000", "sha": "d" * 40})
            return httpx.Response(200, json={"tree": tree, "truncated": truncated})

        marker = "/contents/"
        if marker in path:
            name = path.split(marker, 1)[1]
            if name in failures:
                return httpx.Response(failures[name], json={"message": "nope"})
            if name in files:
                return httpx.Response(200, content=files[name])
            return httpx.Response(404, json={"message": "Not Found"})

        return httpx.Response(500, json={"message": f"예상하지 못한 요청: {path}"})

    return httpx.MockTransport(handler)


def install_github(
    monkeypatch: Any,
    files: dict[str, bytes],
    *,
    expect_token: str | None = GITHUB_TOKEN,
    **kwargs: Any,
) -> None:
    """`runner.build_provider` 를 갈아끼워 `GithubSync` 가 목 transport 를 쓰게 한다.

    ⚠️ `expect_token` 단언이 여기 있는 이유: 목 transport 는 Authorization 헤더를 보지 않으므로,
    이것이 없으면 `integration_service.decrypted_config` 가 **암호문을 그대로** 돌려줘도 모든
    동기화 테스트가 초록이다. 평문 왕복이 동기화 경계까지 실제로 이어지는지를 여기서 붙잡는다.
    """

    def build(provider: str, config: dict[str, Any]) -> GithubSync:
        assert config.get("token") == expect_token, (
            f"동기화에 평문 토큰이 전달되지 않았다: {config.get('token')!r}"
        )
        return GithubSync(
            repo=str(config["repo"]),
            branch=str(config["branch"]),
            path_glob=str(config["path_glob"]),
            token=config.get("token"),
            transport=github_transport(files, **kwargs),
        )

    monkeypatch.setattr(runner, "build_provider", build)


# --------------------------------------------------------------------------------------
# Notion
# --------------------------------------------------------------------------------------
def text_block(block_type: str, text: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"rich_text": [rich_text(text)], **extra}
    return {"id": f"block-{abs(hash((block_type, text)))}", "type": block_type, block_type: body}


def rich_text(text: str, **annotations: Any) -> dict[str, Any]:
    return {
        "plain_text": text,
        "href": annotations.pop("href", None),
        "annotations": {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            **annotations,
        },
    }


class StubNotionClient:
    """`notion-client` 의 `pages.retrieve` · `blocks.children.list` 만 흉내 낸다.

    `missing` 에 넣은 page_id 는 404(`APIResponseError`)를 낸다 — 지워졌거나 공유가 풀린
    페이지다. `page_size` 를 주면 블록 목록을 그 크기로 쪼개 `has_more`/`next_cursor` 를
    돌려준다: 그렇게 하지 않으면 `_list_children` 의 페이지네이션 루프가 **한 번도 실행되지
    않아** 긴 문서의 뒷부분이 잘려도 테스트가 초록으로 지나간다.
    """

    def __init__(
        self,
        pages: dict[str, dict[str, Any]],
        blocks: dict[str, list[dict[str, Any]]],
        *,
        missing: set[str] | None = None,
        page_size: int | None = None,
    ):
        self.pages = _Pages(pages, missing or set())
        self.blocks = _Blocks(blocks, page_size)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _not_found(what: str) -> APIResponseError:
    return APIResponseError(
        code="object_not_found",
        status=404,
        message=f"Could not find {what}",
        headers=httpx.Headers(),
        raw_body_text="{}",
    )


class _Pages:
    def __init__(self, pages: dict[str, dict[str, Any]], missing: set[str]) -> None:
        self._pages = pages
        self._missing = missing

    async def retrieve(self, *, page_id: str) -> dict[str, Any]:
        if page_id in self._missing or page_id not in self._pages:
            raise _not_found(f"page {page_id}")
        return self._pages[page_id]


class _Blocks:
    def __init__(self, blocks: dict[str, list[dict[str, Any]]], page_size: int | None) -> None:
        self.children = _BlockChildren(blocks, page_size)


class _BlockChildren:
    def __init__(self, blocks: dict[str, list[dict[str, Any]]], page_size: int | None) -> None:
        self._blocks = blocks
        self._page_size = page_size

    async def list(
        self, *, block_id: str, start_cursor: str | None = None, page_size: int = 100
    ) -> dict[str, Any]:
        results = self._blocks.get(block_id, [])
        window = self._page_size or max(len(results), 1)
        start = int(start_cursor or 0)
        end = start + window
        has_more = end < len(results)
        return {
            "results": results[start:end],
            "has_more": has_more,
            "next_cursor": str(end) if has_more else None,
        }


def notion_page(page_id: str, title: str, last_edited_time: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "last_edited_time": last_edited_time,
        "properties": {"Name": {"type": "title", "title": [rich_text(title)]}},
    }


def install_notion(
    monkeypatch: Any,
    pages: dict[str, dict[str, Any]],
    blocks: dict[str, list[dict[str, Any]]],
    **kwargs: Any,
) -> StubNotionClient:
    stub = StubNotionClient(pages, blocks, **kwargs)

    def build(provider: str, config: dict[str, Any]) -> NotionSync:
        # `install_github` 과 같은 이유 — 스텁은 토큰을 보지 않으므로 여기서 붙잡는다.
        assert config.get("token") == NOTION_TOKEN, (
            f"동기화에 평문 토큰이 전달되지 않았다: {config.get('token')!r}"
        )
        return NotionSync(
            token=str(config["token"]), page_ids=list(config["page_ids"]), client=stub
        )

    monkeypatch.setattr(runner, "build_provider", build)
    return stub


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------
async def register_github(
    client: AsyncClient,
    owner: Actor,
    project_id: str,
    *,
    token: str | None = GITHUB_TOKEN,
    repo: str = REPO,
    path_glob: str = PATH_GLOB,
    expect_status: int = 201,
) -> dict[str, Any]:
    config: dict[str, Any] = {"repo": repo, "branch": BRANCH, "path_glob": path_glob}
    if token is not None:
        config["token"] = token
    response = await client.post(
        f"{API}/projects/{project_id}/integrations",
        json={"provider": "github", "config": config},
        headers=owner.headers,
    )
    assert response.status_code == expect_status, response.text
    return response.json()


async def register_notion(
    client: AsyncClient,
    owner: Actor,
    project_id: str,
    page_ids: list[str],
    *,
    token: str = NOTION_TOKEN,
    expect_status: int = 201,
) -> dict[str, Any]:
    response = await client.post(
        f"{API}/projects/{project_id}/integrations",
        json={"provider": "notion", "config": {"token": token, "page_ids": page_ids}},
        headers=owner.headers,
    )
    assert response.status_code == expect_status, response.text
    return response.json()


async def trigger_sync(
    client: AsyncClient, owner: Actor, integration_id: str, *, expect_status: int = 202
) -> None:
    """`POST /integrations/{id}/sync` → 202.

    **BackgroundTasks 는 응답 사이클 안에서 끝난다**(ASGI transport) — 이 await 가 돌아오면
    동기화·인제스트·재검토 연쇄가 모두 완료돼 있다 (`pipeline_helpers.ask` 와 같은 전제).
    """
    response = await client.post(f"{API}/integrations/{integration_id}/sync", headers=owner.headers)
    assert response.status_code == expect_status, response.text


async def list_documents(client: AsyncClient, owner: Actor, project_id: str) -> list[dict]:
    response = await client.get(f"{API}/projects/{project_id}/documents", headers=owner.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def sync_events(client: AsyncClient, owner: Actor, project_id: str, integration_id: str):
    response = await client.get(
        f"{API}/projects/{project_id}/events",
        params={"entity_type": "integration", "entity_id": integration_id},
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]
