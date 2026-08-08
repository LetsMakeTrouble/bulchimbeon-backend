"""GitHub 동기화 (`08 §3`) — 공개 레포는 토큰 없이 동작한다 (`05 §5`).

> ### 열거는 트리 API 1회, 본문은 contents API
> `08 §3` 이 지목한 것은 contents API 인데, 그것만으로 `path_glob` 을 적용하려면 디렉터리마다
> 한 번씩 호출해야 한다. 인증 없는 GitHub 은 **시간당 60회**라 문서 몇 개짜리 레포에서도
> 한도가 마른다. 그래서 **열거만** `git/trees?recursive=1`(1회)로 하고 본문은 contents 로
> 받는다 — 같은 REST API 의 다른 엔드포인트이고, 판정 기준(`sha`)은 양쪽이 동일하다.

> ### 변경 감지는 저장된 파일에서 sha 를 다시 계산해 맞춘다
> 원격 트리가 주는 `sha` 는 git blob 해시다. 우리가 저장해 둔 파일로 같은 계산을 하면
> "마지막으로 본 sha" 를 DB 에 들고 있지 않아도 비교가 성립한다 (`base.git_blob_sha`).
> 커서를 두지 않으므로 문서를 되돌리거나 지웠을 때 커서만 앞서 나가는 사고가 없다.
"""

import asyncio
import logging
import re
from pathlib import Path

import httpx

from app.services.document_service import safe_filename
from app.services.sync.base import (
    PartialListing,
    RemoteRef,
    StoredVersion,
    SyncError,
    git_blob_sha,
)
from app.utils.parsing import EXTENSION_TO_MIME

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
SOURCE_TYPE = "github"

# GitHub 이 권장하는 버전 고정 헤더. 붙이지 않으면 기본 버전이 바뀔 때 응답 모양이 달라진다.
_API_VERSION = "2022-11-28"
_TIMEOUT_SECONDS = 20.0

# 트리 응답에서 파일을 뜻하는 타입. `tree`(디렉터리) · `commit`(서브모듈)은 제외한다.
_BLOB = "blob"

# 한 번의 동기화가 만들 수 있는 문서 수 상한이 아니라, 응답이 잘렸는지를 알리기 위한 표식이다.
_TRUNCATED_MESSAGE = (
    "레포가 너무 커서 GitHub 이 파일 목록을 잘라서 돌려줬습니다. path_glob 을 더 좁혀 주세요."
)


def split_repo(repo: str) -> tuple[str, str]:
    """`owner/name` 을 쪼갠다. 형식 검증은 `schemas/integration.GithubConfig` 가 이미 했다."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise SyncError(f"레포 형식이 올바르지 않습니다: {repo!r} (owner/name 이어야 합니다)")
    return owner, name


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`docs/**/*.md` 같은 경로 글롭을 정규식으로 옮긴다.

    `fnmatch` 를 쓰지 않는 이유: `fnmatch` 의 `*` 는 `/` 까지 삼켜서 `*.md` 가 하위 디렉터리
    파일까지 전부 매칭한다. 담당자가 최상위 md 만 넣으려고 `*.md` 를 적었는데 레포 전체가
    딸려 오는 사고가 난다. 여기서는 git 계열 글롭과 같은 뜻으로 옮긴다:

    - `**/` → 디렉터리 0개 이상   - `**` → 아무거나
    - `*` → `/` 를 제외한 0글자 이상   - `?` → `/` 를 제외한 1글자
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


class GithubSync:
    """`08 §3` 의 provider. 인스턴스 하나가 연동 하나의 한 번의 동기화를 담당한다."""

    source_type = SOURCE_TYPE

    def __init__(
        self,
        *,
        repo: str,
        branch: str,
        path_glob: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`transport` 는 테스트가 `httpx.MockTransport` 를 넣는 자리다.

        클라이언트 자체가 아니라 transport 를 받는 이유: 헤더·타임아웃·base_url 같은 **실제
        요청 구성**이 테스트에서도 그대로 쓰여야 하기 때문이다. 클라이언트를 통째로 주입하면
        그 부분이 테스트에서 빠져 운영에서만 틀린다.
        """
        self._owner, self._name = split_repo(repo)
        self._repo = repo
        self._branch = branch
        self._matcher = glob_to_regex(path_glob)
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            timeout=_TIMEOUT_SECONDS,
            headers=self._headers(token),
            transport=transport,
        )

    @staticmethod
    def _headers(token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- 열거 ---------------------------------------------------------------------------
    async def list_documents(self) -> list[RemoteRef]:
        """`path_glob` 에 맞는 파일을 열거한다. 본문은 받지 않는다."""
        payload = await self._get_json(
            f"/repos/{self._owner}/{self._name}/git/trees/{self._branch}",
            params={"recursive": "1"},
        )

        refs: list[RemoteRef] = []
        for entry in payload.get("tree", []):
            if entry.get("type") != _BLOB:
                continue
            path = str(entry.get("path", ""))
            if not self._matcher.match(path):
                continue
            suffix = Path(path).suffix.lower()
            mime = EXTENSION_TO_MIME.get(suffix)
            if mime is None:
                # 글롭이 넓게 잡혀 이미지·바이너리가 걸린 경우다. 파서가 없으므로 조용히 뺀다 —
                # 실패로 세면 매 동기화마다 같은 파일이 실패 목록을 채운다.
                logger.debug("지원하지 않는 확장자라 건너뛴다: %s", path)
                continue
            refs.append(
                RemoteRef(
                    source_ref=f"{self._repo}:{path}",
                    # 제목은 **경로 전체**다 — 레포에 `README.md` 가 여럿일 때 파일명만으로는
                    # 목록에서 구분되지 않는다.
                    title=path,
                    filename=safe_filename(path, suffix),
                    mime=mime,
                    revision=str(entry.get("sha", "")),
                )
            )

        if payload.get("truncated"):
            # 조용히 자르지 않는다 — 받아 온 만큼은 처리하고 호출자가 실패 목록에 남긴다.
            # `source_ref` 를 `*` 로 두는 것은 "특정 파일이 아니라 목록 자체가 잘렸다"는 뜻이다.
            raise PartialListing(refs, [("*", _TRUNCATED_MESSAGE)])
        return refs

    # --- 변경 판정 ----------------------------------------------------------------------
    async def is_unchanged(self, ref: RemoteRef, stored: StoredVersion) -> bool:
        """저장된 파일의 git blob sha 가 원격 sha 와 같으면 변경 없음 (`08 §3`).

        파일 읽기는 블로킹이므로 스레드로 밀어낸다 (룰 8).
        """
        if not ref.revision:
            return False

        def _work() -> str | None:
            if not stored.path.exists():
                return None
            return git_blob_sha(stored.path.read_bytes())

        local = await asyncio.get_running_loop().run_in_executor(None, _work)
        return local is not None and local == ref.revision

    # --- 본문 ---------------------------------------------------------------------------
    async def fetch(self, ref: RemoteRef) -> bytes:
        """contents API 로 원본 바이트를 받는다 (`Accept: …raw` 라 base64 디코딩이 필요 없다)."""
        _, _, path = ref.source_ref.partition(":")
        response = await self._client.get(
            f"/repos/{self._owner}/{self._name}/contents/{path}",
            params={"ref": self._branch},
            headers={"Accept": "application/vnd.github.raw"},
        )
        self._raise_for_status(response, what=path)
        return response.content

    # --- HTTP ---------------------------------------------------------------------------
    async def _get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict:
        response = await self._client.get(url, params=params)
        self._raise_for_status(response, what=f"{self._repo}@{self._branch}")
        return response.json()

    def _raise_for_status(self, response: httpx.Response, *, what: str) -> None:
        """실패를 **담당자에게 보여줄 수 있는 문장**으로 바꾼다 (`base.SyncError` 독스트링).

        httpx 예외 문자열에는 요청 URL 과 헤더 정보가 섞이므로 그대로 올리지 않는다 (`03 §7`).
        """
        if response.is_success:
            return

        status = response.status_code
        if status in (401, 403):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise SyncError(
                    "GitHub API 호출 한도를 초과했습니다. 잠시 뒤에 다시 시도하거나 "
                    "연동에 토큰을 넣어 주세요."
                )
            raise SyncError("GitHub 접근이 거부됐습니다. 토큰과 레포 권한을 확인해 주세요.")
        if status == 404:
            raise SyncError(f"GitHub 에서 찾을 수 없습니다: {what}")
        logger.warning("github 응답 오류 %s: %s", status, response.text[:200])
        raise SyncError(f"GitHub 응답 오류({status})로 동기화하지 못했습니다.")
