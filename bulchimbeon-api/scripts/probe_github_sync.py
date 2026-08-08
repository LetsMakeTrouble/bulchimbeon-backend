"""실 GitHub 공개 레포 e2e — M8 DoD (`prompts/08-integrations.md` 완료 기준).

목킹 테스트(`tests/test_sync_github.py`)가 증명할 수 **없는 것 하나**를 확인한다:
GitHub 이 실제로 돌려주는 응답이 우리가 가정한 모양인가.

구체적으로 세 가지다.

1. **트리 API 응답 모양** — `tree[].path` · `type` · `sha` 가 있고 `recursive=1` 이 먹는가.
2. ⭐ **sha 의 정체** — 원격 `sha` 가 git blob 해시라는 전제가 맞는가.
   이게 틀리면 변경 감지가 통째로 무너져 **매 동기화가 같은 파일을 새 버전으로 다시 만든다**
   (= 재검토 연쇄가 헛돌고 담당자에게 가짜 카드가 쌓인다). 목킹은 우리가 만든 sha 를
   우리가 다시 확인하는 것이라 이 전제를 검증하지 못한다.
3. **contents API 의 raw 응답** — `Accept: …raw` 로 base64 가 아닌 원본 바이트가 오는가.

DB 도 앱도 건드리지 않는다 — `GithubSync` 만 실제 네트워크로 돌린다.

실행
----
    uv run python scripts/probe_github_sync.py <owner/repo> [--branch main] [--path-glob '**/*.md']

    # 비공개 레포나 호출 한도(인증 없으면 시간당 60회)가 걱정되면 토큰을 준다
    GITHUB_TOKEN=ghp_... uv run python scripts/probe_github_sync.py <owner/repo>

종료 코드는 0(통과)/1(실패)이다.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.sync.base import StoredVersion, SyncError, git_blob_sha  # noqa: E402
from app.services.sync.github_sync import GithubSync  # noqa: E402

MAX_PREVIEW = 5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="실 GitHub 공개 레포 동기화 프로브 (M8 DoD)")
    parser.add_argument("repo", help="owner/name (예: acme/partner-docs)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--path-glob", default="**/*.md")
    return parser.parse_args()


async def probe(repo: str, branch: str, path_glob: str, token: str | None) -> int:
    lines: list[str] = []

    def log(message: str = "") -> None:
        print(message)
        lines.append(message)

    log(f"# M8 GitHub 실 레포 프로브 — {datetime.now(UTC).isoformat(timespec='seconds')}")
    log(f"repo={repo} branch={branch} path_glob={path_glob!r} token={'있음' if token else '없음'}")
    log()

    provider = GithubSync(repo=repo, branch=branch, path_glob=path_glob, token=token)
    failures: list[str] = []
    try:
        # ① 열거 -------------------------------------------------------------------------
        try:
            refs = await provider.list_documents()
        except SyncError as exc:
            log(f"❌ ① 열거 실패: {exc}")
            return 1

        log(f"① 열거: {len(refs)}개 매칭")
        for ref in refs[:MAX_PREVIEW]:
            log(f"   - {ref.source_ref}  sha={ref.revision[:12]}  → {ref.filename}")
        if len(refs) > MAX_PREVIEW:
            log(f"   … 외 {len(refs) - MAX_PREVIEW}개")
        if not refs:
            log("❌ 매칭된 파일이 없다. --path-glob 을 확인하라.")
            return 1
        if not all(ref.revision for ref in refs):
            failures.append("① 트리 응답에 sha 가 없는 항목이 있다")
        log()

        # ② 본문 + sha 대조 ---------------------------------------------------------------
        target = refs[0]
        content = await provider.fetch(target)
        local_sha = git_blob_sha(content)
        log(f"② 본문: {target.source_ref} — {len(content)} bytes")
        first_line = content.split(b"\n", 1)[0][:80].decode("utf-8", errors="replace")
        log(f"   첫 줄: {first_line!r}")
        log(f"   원격 sha={target.revision}")
        log(f"   로컬 sha={local_sha}")
        if local_sha == target.revision:
            log("   ✅ git blob 해시가 일치한다 — 변경 감지의 전제가 성립한다")
        else:
            failures.append("② 원격 sha 가 git blob 해시가 아니다 — 변경 감지가 무너진다")
            log("   ❌ 불일치")
        log()

        # ③ 변경 판정 ---------------------------------------------------------------------
        with tempfile.TemporaryDirectory() as directory:
            same = Path(directory) / "same.md"
            same.write_bytes(content)
            changed = Path(directory) / "changed.md"
            changed.write_bytes(content + b"\n<!-- edited -->\n")

            now = datetime.now(UTC)
            unchanged = await provider.is_unchanged(
                target, StoredVersion(version_id=uuid4(), path=same, created_at=now)
            )
            differs = await provider.is_unchanged(
                target, StoredVersion(version_id=uuid4(), path=changed, created_at=now)
            )

        log(f"③ 변경 판정: 같은 내용 → unchanged={unchanged} (기대 True)")
        log(f"             바뀐 내용 → unchanged={differs} (기대 False)")
        if not unchanged:
            failures.append("③ 같은 내용인데 변경으로 판정한다 — 매 동기화가 새 버전을 만든다")
        if differs:
            failures.append("③ 바뀐 내용인데 변경을 못 잡는다 — 룰 5 가 발화하지 않는다")
        log()
    finally:
        await provider.aclose()

    if failures:
        log("## 실패")
        for failure in failures:
            log(f" - {failure}")
        _save(lines)
        return 1

    log("## ✅ 통과 — 실 GitHub 응답이 구현의 전제와 일치한다")
    _save(lines)
    return 0


def _save(lines: list[str]) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    path = Path(__file__).resolve().parent.parent / f"probe-github-sync-{stamp}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n결과 저장: {path}")


def main() -> int:
    args = _parse_args()
    return asyncio.run(
        probe(args.repo, args.branch, args.path_glob, os.environ.get("GITHUB_TOKEN") or None)
    )


if __name__ == "__main__":
    raise SystemExit(main())
