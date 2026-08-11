"""일일 LLM 호출 상한 (`06 §6`, 룰 1).

`settings.daily_llm_call_limit` 을 넘으면 질문 접수는 받되 **강제 🔴 `quota_exceeded`** 로
떨어뜨린다. ⚠️ 이 값은 env 가 아니라 **`projects.settings`** 다 — 임계값 하드코딩 금지
원칙의 일관성 때문이다 (`04 §3`).

> ### 카운터는 이제 **DB** 다 (2026-08-11, 운영 전환 항목 B)
> 종전에는 프로세스 메모리 딕셔너리라 **재시작하면 0 으로 돌아갔다.** 하루에 배포를 세 번
> 하면 그날 상한이 사실상 세 배가 됐고, "누가 얼마나 썼나"에도 답할 수 없었다.
> 지금은 `llm_usage` 테이블의 오늘(UTC) 행 수를 센다 — 재시작에도 유지되고 같은 데이터가
> 비용 집계에도 쓰인다.
>
> ⚠️ **진행 중인 호출은 아직 안 세어진다.** `llm_usage` 는 파이프라인이 끝날 때 한 번에
> 적재되므로(`services/llm/usage` — 커넥션을 아끼려는 선택), 동시에 도는 질문들은 서로의
> 소비를 보지 못한 채 상한을 통과할 수 있다. 동시 실행은 세마포어로 묶여 있으므로
> 최대 그 수만큼만 새어 나가고, 상한의 성격이 "회계"가 아니라 **"폭주 방어"** 라서 이
> 오차를 허용한다. 정확한 차단이 필요해지면 그때는 상한을 토큰·비용 기준으로 바꿔야 한다.

> ### `500` 이라는 기본값에 대하여
> 이 숫자는 최초 설계 리뷰에서 붙은 **자리값**이고 산출 근거가 문서 어디에도 없다.
> 2026-08-11 실측으로 환산한 의미는 이렇다: 질문 1건이 평균 **2.94 호출**이므로
> 500 ≈ **하루 170 질문**, 비용으로는 질문당 $0.0098 기준 **약 $1.7/일**(프로젝트당)이다.
> 바꿀 때는 이 환산을 함께 갱신하라 (`03 §4.2`).
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage import LLMUsage


def _today_start() -> datetime:
    """오늘(UTC) 0시. 프로젝트 타임존이 아니라 UTC 로 자르는 것은 종전 구현과 같다 —
    상한은 사용자에게 보이는 값이 아니라 폭주 방어선이라 경계의 정확한 위치가 중요하지 않다."""
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def used(db: AsyncSession, project_id: UUID) -> int:
    """오늘(UTC) 이 프로젝트가 쓴 LLM 호출 수."""
    count = await db.scalar(
        select(func.count())
        .select_from(LLMUsage)
        .where(LLMUsage.project_id == project_id, LLMUsage.created_at >= _today_start())
    )
    return int(count or 0)


async def is_exceeded(db: AsyncSession, project_id: UUID, limit: int) -> bool:
    """상한 초과 여부. `limit` 은 `projects.settings.daily_llm_call_limit` 이다."""
    return await used(db, project_id) >= limit


async def set_used(db: AsyncSession, project_id: UUID, count: int) -> None:
    """테스트가 `quota_exceeded` 분기를 재현하기 위한 진입점.

    실제 행을 넣는다 — 카운터가 DB 파생값이라 다른 방법이 없고, 그 덕에 테스트가
    운영과 **같은 경로**를 탄다. 모델·단계는 합성값이고 비용은 0 이다.
    """
    db.add_all(
        [
            LLMUsage(
                project_id=project_id,
                step="test_seed",
                model="test",
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                cost_usd=0,
            )
            for _ in range(count)
        ]
    )
    await db.flush()


async def reset(db: AsyncSession, project_id: UUID) -> None:
    """테스트 격리용 — 오늘 치 행을 지운다. 운영 코드에서 부르지 않는다."""
    await db.execute(
        delete(LLMUsage).where(
            LLMUsage.project_id == project_id, LLMUsage.created_at >= _today_start()
        )
    )
    await db.flush()


__all__ = ["is_exceeded", "reset", "set_used", "used"]


# ⚠️ `consume()` 은 없어졌다. 호출 수는 `llm_usage` 행 수에서 파생되므로 따로 세지 않는다.
#    종전 호출 지점(`_Ctx.call_json`)에서 제거됐다.
