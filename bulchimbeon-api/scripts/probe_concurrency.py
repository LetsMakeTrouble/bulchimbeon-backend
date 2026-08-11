"""동시 질문 부하 재현 — 운영 전환 항목 A 의 검증 (AC-A5).

테스트 스위트로는 못 잡는다. 테스트는 커넥션 **하나**를 공유하며 롤백으로 격리하므로
(`03 §5.3`) 커넥션 풀 고갈이 성립하지 않는다. 그래서 실제로 뜬 서버에 던진다.

무엇을 보는가:

1. **질문 접수(202)가 전부 성공하는가** — 종전 실패 모드는 "답변이 느려진다"가 아니라
   **새 질문을 아예 못 낸다** 였다. 진행 중 파이프라인이 커넥션을 15초씩 붙잡고,
   접수 경로(`get_db`)가 같은 풀을 쓰기 때문이다.
2. **파이프라인이 데드라인을 넘기지 않는가** — 상한 대기가 시계에 섞이면 여기서 드러난다.
3. **끝까지 처리되는가** — `processing` 에 남은 건이 없어야 한다.

```bash
set -a; . ./.env; set +a
uv run python scripts/probe_concurrency.py --base https://… --project <UUID> \\
    --email asker@example --password '…' --count 30
```

⚠️ **실 LLM 비용이 든다.** 질문 1건당 약 $0.01 이므로 30건이면 $0.3 정도다.
⛔ 데모 프로젝트에 대고 돌리지 마라 — 질문·답변·카드가 그대로 쌓인다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402


async def _login(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    response = await client.post(
        f"{base}/api/v1/auth/login", json={"email": email, "password": password}
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def _ask(
    client: httpx.AsyncClient, base: str, token: str, project_id: str, index: int
) -> tuple[int, str | None, float]:
    """질문 1건 접수. 돌려주는 값은 (상태코드, question_id, 접수 소요초)."""
    started = time.monotonic()
    try:
        response = await client.post(
            f"{base}/api/v1/projects/{project_id}/questions",
            json={"content_ko": f"환불 기한이 며칠인가요? (부하 검증 {index})"},
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        print(f"  [{index:02d}] 접수 실패: {exc}")
        return 0, None, time.monotonic() - started

    elapsed = time.monotonic() - started
    if response.status_code != 202:
        print(f"  [{index:02d}] 접수 HTTP {response.status_code}: {response.text[:120]}")
        return response.status_code, None, elapsed
    return 202, str(response.json()["question_id"]), elapsed


async def _await_settled(
    client: httpx.AsyncClient, base: str, token: str, question_id: str, timeout_s: float
) -> dict[str, object]:
    """`processing` 을 벗어날 때까지 폴링한다."""
    deadline = time.monotonic() + timeout_s
    headers = {"Authorization": f"Bearer {token}"}
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"{base}/api/v1/questions/{question_id}", headers=headers)
        if response.status_code == 200:
            body = response.json()
            if body.get("status") != "processing":
                return body
        await asyncio.sleep(1.0)
    return body


async def main() -> int:
    parser = argparse.ArgumentParser(description="동시 질문 부하 재현")
    parser.add_argument("--base", required=True, help="서버 주소 (https://…)")
    parser.add_argument("--project", required=True, help="측정용 프로젝트 UUID (데모 금지)")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--count", type=int, default=30, help="동시에 던질 질문 수")
    parser.add_argument("--settle-timeout", type=float, default=180.0)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    limits = httpx.Limits(max_connections=args.count + 10)
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        token = await _login(client, base, args.email, args.password)

        print(f"동시 질문 {args.count}건 접수 시작")
        started = time.monotonic()
        results = await asyncio.gather(
            *(_ask(client, base, token, args.project, i) for i in range(args.count))
        )
        accepted_in = time.monotonic() - started

        codes = [code for code, _, _ in results]
        question_ids = [qid for _, qid, _ in results if qid]
        accept_times = [seconds for _, _, seconds in results]

        print()
        print("── 1. 접수 ──────────────────────────────────────")
        print(f"  202: {codes.count(202)} / {args.count}")
        print(f"  전체 접수 소요: {accepted_in:.1f}s · 개별 최대 {max(accept_times):.1f}s")
        if codes.count(202) != args.count:
            other = sorted({code for code in codes if code != 202})
            print(f"  ⛔ 202 가 아닌 응답: {other} — 접수가 막혔다 (풀 고갈 의심)")

        print()
        print("── 2. 처리 완료 대기 ────────────────────────────")
        settled = await asyncio.gather(
            *(_await_settled(client, base, token, qid, args.settle_timeout) for qid in question_ids)
        )

    statuses = [str(body.get("status", "unknown")) for body in settled]
    stuck = statuses.count("processing") + statuses.count("unknown")
    print(f"  완료 {len(statuses) - stuck} / {len(statuses)}")
    for status in sorted(set(statuses)):
        print(f"    {status}: {statuses.count(status)}")

    print()
    print("── 판정 ─────────────────────────────────────────")
    ok = codes.count(202) == args.count and stuck == 0
    if ok:
        print("  ✅ 접수 전건 성공 · 정체 0건")
    else:
        print("  ❌ 실패 — 접수가 막혔거나 처리되지 못한 건이 있다")
        print("     `pipeline_max_concurrency` 와 `db_pool_size` 의 관계를 확인하라 (`03 §8`)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
