"""외부 연동 스키마 (`05 §5` 와 1:1).

계약서 §5 가 규정하는 것:
- 등록 요청 `{provider: "notion"|"github", config}`
- config 예시 — notion `{token, page_ids}` / github `{token?, repo, branch, path_glob}`
  (github 은 "public repo 면 token 생략 가능")
- 목록 응답 "목록 + last_synced_at·상태 (**토큰은 마스킹**)"

여기 있는 필드는 전부 위 규정 또는 `04 §2` integrations 의 컬럼에서 온 것이다.
계약서에 없는 필드를 새로 만들지 않는다.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.integration import PROVIDER_GITHUB, PROVIDER_NOTION

Provider = Literal["notion", "github"]
SyncStatus = Literal["ok", "failed"]

# github config 기본값 — `05 §5` 예시는 넷을 모두 주지만, 레포만으로도 등록할 수 있게 한다.
# 계약서가 요구한 값(예시의 `main`·`docs/**/*.md`)을 못 쓰게 막지 않으므로 계약 확장이 아니다.
DEFAULT_BRANCH = "main"
DEFAULT_PATH_GLOB = "**/*.md"


class NotionConfig(BaseModel):
    """`05 §5` — `{token: "ntn_...", page_ids: ["..."]}`.

    Notion API 는 공개 페이지라도 integration 토큰을 요구하므로 `token` 은 필수다
    (GitHub 과 다른 점이다).
    """

    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1)]
    page_ids: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class GithubConfig(BaseModel):
    """`05 §5` — `{token?: "ghp_...", repo, branch, path_glob}`. **공개 레포면 token 생략 가능.**"""

    model_config = ConfigDict(extra="forbid")

    token: str | None = None
    # `owner/name`. 형식 검증은 `github_sync.split_repo` 가 하는 판정과 같은 뜻이다.
    repo: Annotated[str, Field(min_length=3, pattern=r"^[^/\s]+/[^/\s]+$")]
    branch: Annotated[str, Field(min_length=1)] = DEFAULT_BRANCH
    path_glob: Annotated[str, Field(min_length=1)] = DEFAULT_PATH_GLOB


CONFIG_MODELS: dict[str, type[BaseModel]] = {
    PROVIDER_NOTION: NotionConfig,
    PROVIDER_GITHUB: GithubConfig,
}

# 마스킹 대상 = 그 provider config 의 비밀 필드. **여기가 정의의 단일 위치**이며
# `integration_service` 의 저장·복호화와 `crypto.mask` 가 이 목록만 본다.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    PROVIDER_NOTION: ("token",),
    PROVIDER_GITHUB: ("token",),
}


class IntegrationCreate(BaseModel):
    """`POST /projects/{id}/integrations` 요청.

    `config` 를 provider 별 모델로 검증한다 — 여기서 `ValueError` 를 올리면 FastAPI 가
    `RequestValidationError` 로 감싸고 전역 핸들러가 **400 `VALIDATION_ERROR`** 로 바꾼다
    (`main.py`). 계약서 §1.4 표 안의 코드이므로 새 코드를 만들지 않는다.
    """

    provider: Provider
    config: dict[str, Any]

    @model_validator(mode="after")
    def _config_matches_provider(self) -> "IntegrationCreate":
        self.parsed_config()
        return self

    def parsed_config(self) -> BaseModel:
        """provider 에 맞는 config 모델 인스턴스. 서비스가 타입 있는 값을 읽는 통로다."""
        try:
            return CONFIG_MODELS[self.provider].model_validate(self.config)
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(str(part) for part in error["loc"]) for error in exc.errors()
            )
            raise ValueError(f"{self.provider} config 가 올바르지 않습니다: {fields}") from exc


class IntegrationOut(BaseModel):
    """목록 아이템 (`05 §5`) — **`config.token` 은 마스킹된 값**이다.

    `04 §2` integrations 의 컬럼 사영이며, 평문 토큰은 어떤 응답에도 실리지 않는다.

    `created_at` 만 `04 §2` 의 컬럼 목록이 아니라 **전 테이블 공통 규약**(`04` 문서 상단,
    `TimestampMixin`)에서 온다. 계약서 §5 가 "목록"이라고만 적은 자리에 정렬·표시 기준이
    하나는 있어야 해서 실었다.
    """

    id: UUID
    provider: Provider
    config: dict[str, Any]
    last_synced_at: datetime | None
    last_sync_status: SyncStatus | None
    created_at: datetime


class IntegrationListResponse(BaseModel):
    items: list[IntegrationOut]
