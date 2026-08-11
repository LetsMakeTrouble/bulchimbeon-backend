"""동시 파이프라인 상한 (운영 전환 항목 A).

> ### 왜 필요한가 — 15건에서 **접수까지** 막힌다
> 파이프라인은 DB 세션을 **LLM 3~4회를 도는 내내** 붙잡는다(`answer.run_answer_pipeline`).
> 실측 소요는 5~15초다. 커넥션 풀이 다 차면 새 질문 접수(`get_db`)가 **같은 풀**을 쓰기
> 때문에 접수 자체가 대기에 걸린다 — 사용자에게는 서비스가 멈춘 것으로 보인다.
> 상한을 두면 초과분은 실패가 아니라 **대기**하고, 풀에는 요청 처리용 여유가 남는다.

> ### 왜 풀만 키우지 않는가
> 풀을 키우면 DB 는 버티지만 OpenAI 동시 호출이 무제한이 된다. 조직 rate limit(429)은
> `LLMProviderError` 로 올라와 **파이프라인 총 실패(D23)** 가 되므로, 질문이 통째로
> `failed` 로 떨어진다. 세마포어 하나가 DB 커넥션과 OpenAI 동시성을 **함께** 묶는다.

> ### ⚠️ 획득은 반드시 **데드라인 시계보다 먼저**다
> `_Ctx.started` 는 컨텍스트 생성 시점의 `time.monotonic()` 이고 데드라인(🟢🟡 25초 ·
> 🔴 35초)이 여기서 잰다. 슬롯을 기다린 시간이 그 시계에 포함되면 **줄을 섰다는 이유로
> 데드라인을 넘겨** 멀쩡한 답변이 🟡 로 강등된다. 그래서 대기 → 세션 → 시계 순서다.

세마포어는 **이벤트 루프별**로 만든다. `asyncio.Semaphore` 는 첫 사용 시 루프에 묶이고
다른 루프에서 쓰면 죽는데, 테스트는 테스트마다 새 루프를 연다.
"""

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import settings

logger = logging.getLogger(__name__)

# 슬롯을 이만큼 이상 기다렸으면 남긴다. 상한이 실제로 병목인지 보는 유일한 신호다.
SLOW_WAIT_SECONDS = 1.0

_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(1, settings.pipeline_max_concurrency))
        _semaphores[loop] = semaphore
    return semaphore


@asynccontextmanager
async def pipeline_slot() -> AsyncIterator[None]:
    """파이프라인 1건의 실행 슬롯. 초과분은 여기서 **대기**한다(실패가 아니다).

    ⛔ 이 블록 **안에서** DB 세션을 열고 컨텍스트를 만들어라. 밖에서 열면 대기 중인
    질문들이 커넥션을 쥔 채 줄을 서게 되어 상한을 두는 의미가 사라진다.
    """
    semaphore = _semaphore()
    if semaphore.locked():
        waited_from = asyncio.get_running_loop().time()
        await semaphore.acquire()
        waited = asyncio.get_running_loop().time() - waited_from
        if waited >= SLOW_WAIT_SECONDS:
            logger.warning(
                "파이프라인 슬롯 대기 %.1fs — 상한(%s)이 병목이다",
                waited,
                settings.pipeline_max_concurrency,
            )
    else:
        await semaphore.acquire()

    try:
        yield
    finally:
        semaphore.release()


def reset() -> None:
    """테스트가 상한을 바꿔 끼울 때 쓴다. 운영 코드에서 부르지 않는다."""
    _semaphores.clear()
