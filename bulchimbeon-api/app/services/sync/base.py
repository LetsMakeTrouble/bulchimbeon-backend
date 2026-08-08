"""동기화 provider 공통 계약 (`08 §3`·`§4`).

provider 가 아는 것은 **원격**뿐이다: 무엇이 있는지(`list_documents`), 우리가 가진 것과
같은지(`is_unchanged`), 그 내용이 무엇인지(`fetch`). 문서·버전·인제스트·재검토 연쇄는
전부 `runner.py` 가 기존 서비스로 처리한다.

> ### 변경 감지 방식이 provider 마다 다르다 — 그래서 판정이 provider 쪽에 있다
> - GitHub: **파일 SHA 비교** (`08 §3`). 저장된 파일에서 git blob sha 를 다시 계산해
>   원격 sha 와 맞춘다 — 마지막으로 본 sha 를 따로 저장하지 않아도 되므로 상태가 어긋날 일이
>   없다.
> - Notion: **`last_edited_time` 비교** (`08 §4`). 우리가 그 페이지를 마지막으로 받아 온
>   시각(= 최신 버전의 `created_at`)보다 뒤에 편집됐으면 바뀐 것이다.
>
> 둘 다 `integrations` 에 커서 컬럼을 두지 않는다. 커서를 두면 문서를 지우거나 되돌렸을 때
> 커서와 실제 문서가 어긋나 "바뀌었는데 안 가져온다"가 조용히 생긴다.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class RemoteRef:
    """원격 문서 하나의 식별 정보. 본문은 담지 않는다 — 바뀐 것만 내려받기 위해서다."""

    # `04 §2` documents.source_ref — github `owner/repo:path`, notion page_id.
    source_ref: str
    title: str
    # 저장 파일명 (`document_service.safe_filename` 을 이미 통과한 값).
    filename: str
    mime: str
    # 이벤트 payload·로그에 남기는 개정 식별자. github 은 blob sha, notion 은 편집 시각이다.
    revision: str


@dataclass(frozen=True)
class StoredVersion:
    """우리가 이미 가지고 있는 최신 버전. 변경 판정에 필요한 만큼만 원시값으로 담는다.

    ORM 객체를 provider 로 넘기지 않는다 — 판정은 세션 밖(네트워크 구간)에서 일어난다.
    """

    version_id: UUID
    path: Path
    created_at: datetime
    # 직전 인제스트가 실패한 버전인가. `True` 면 "내용은 그대로인데 검색에 못 들어간 상태"이므로
    # 같은 버전을 다시 태워야 한다 (`runner._sync_one`) — 새 버전을 만들면 깨진 파일 하나가
    # 동기화마다 버전을 하나씩 쌓는다.
    ingest_failed: bool = False


class SyncProvider(Protocol):
    """`runner.py` 가 provider 에게 요구하는 전부."""

    source_type: str

    async def list_documents(self) -> list[RemoteRef]:
        """원격에서 대상 문서를 열거한다. 본문은 내려받지 않는다."""
        ...

    async def is_unchanged(self, ref: RemoteRef, stored: StoredVersion) -> bool:
        """이미 가진 버전과 같은가. `True` 면 내려받지도, 새 버전을 만들지도 않는다."""
        ...

    async def fetch(self, ref: RemoteRef) -> bytes:
        """본문 바이트. 텍스트 계열은 UTF-8 로 인코딩된 값이다."""
        ...

    async def aclose(self) -> None:
        """HTTP 클라이언트 정리."""
        ...


class SyncError(RuntimeError):
    """동기화를 계속할 수 없는 오류 (인증 실패·레포 없음 등).

    ⚠️ 메시지는 **담당자에게 보여줄 수 있는 문장**이어야 한다. 예외 원문을 그대로 담지 않는다 —
    httpx 예외는 URL 에 토큰이 붙은 요청 정보를 문자열에 싣는다 (`03 §7`,
    `pipeline/ingest._user_facing_error` 와 같은 판단).
    """


class PartialListing(SyncError):
    """열거가 **일부만** 성공했다 — 받아 온 만큼은 처리하고 나머지는 실패로 남긴다 (`08 §6`).

    실패 격리는 "본문 수집"만의 규칙이 아니다. Notion 은 `page_ids` 를 한 건씩 조회하므로
    페이지 하나가 지워지거나 공유가 풀리면 **열거 단계에서** 터지는데, 그것을 통째로 fatal 로
    올리면 멀쩡한 나머지 페이지가 한 건도 들어오지 않는다 — 담당자가 config 를 직접 고치기
    전까지 그 연동은 영구히 죽는다.

    `failures` 는 `(source_ref, 담당자에게 보여줄 메시지)` 목록이다.
    """

    def __init__(self, refs: list[RemoteRef], failures: list[tuple[str, str]]) -> None:
        super().__init__("일부 항목을 열거하지 못했습니다.")
        self.refs = refs
        self.failures = failures


@dataclass
class SyncOutcome:
    """한 번의 동기화 결과. 그대로 `sync.run` 이벤트 payload 가 된다 (사용자 결정 2026-08-08)."""

    scanned: int = 0
    new_documents: int = 0
    new_versions: int = 0
    unchanged: int = 0
    skipped: int = 0
    # 직전 실행에서 인제스트가 실패했던 버전을 같은 버전으로 다시 태워 살린 건수.
    # 새 문서도 새 버전도 아니므로 SSE payload(`05 §12.3` 고정 3필드)에는 들어가지 않는다.
    repaired: int = 0
    # 저장 파일이 사라져 변경 판정을 못 하고 다시 받아 온 건수 (`runner._sync_one`).
    restored: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    # 열거 자체가 실패하면 파일 단위 결과가 없다 — 그때만 값이 있다.
    fatal: str | None = None

    def fail(self, source_ref: str, message: str) -> None:
        self.failed.append({"source_ref": source_ref, "error": message})

    @property
    def succeeded(self) -> bool:
        """`integrations.last_sync_status` 와 알림 종류를 정하는 판정.

        **파일 하나가 실패해도 실행은 성공**이다 — 실패 격리(`08 §6`)의 뜻이 그것이고,
        실패 목록은 이벤트 payload 에 남는다. 다만 시도한 것이 전부 실패했으면 성공이 아니다:
        토큰이 죽었거나 경로가 통째로 잘못된 경우가 그 모양으로 나타난다.
        """
        if self.fatal is not None:
            return False
        succeeded = self.new_documents + self.new_versions + self.repaired
        attempted = succeeded + len(self.failed)
        return not (attempted > 0 and succeeded == 0)

    def to_payload(self, *, provider: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": provider,
            "status": "ok" if self.succeeded else "failed",
            "scanned": self.scanned,
            "new_documents": self.new_documents,
            "new_versions": self.new_versions,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "repaired": self.repaired,
            "restored": self.restored,
            "failed": self.failed,
        }
        if self.fatal is not None:
            payload["fatal"] = self.fatal
        return payload


def git_blob_sha(payload: bytes) -> str:
    """git 의 blob 오브젝트 해시 — GitHub 이 트리·contents 응답에 싣는 `sha` 와 같은 값이다.

    `sha1("blob {길이}\\0" + 내용)` 이 정의다. 저장된 파일에서 이 값을 다시 계산할 수 있으므로
    "마지막으로 본 sha" 를 DB 에 들고 있지 않아도 변경을 판정할 수 있다 (모듈 독스트링).

    보안 해시로 쓰는 것이 아니라 **git 과 같은 값을 만들기 위한** 계산이다 — 그래서 sha1 이고
    `usedforsecurity=False` 다.
    """
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()
