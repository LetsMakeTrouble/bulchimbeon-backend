"""LLM 사용량 수집 — 프로바이더가 부르고, 스코프가 끝날 때 DB 로 넘긴다.

> ### 왜 `ContextVar` 인가
> 비용을 귀속시키려면 "이 호출이 어느 프로젝트·누구·어떤 질문의 것인가"를 알아야 하는데,
> 그 정보는 **프로바이더가 모른다**. 프로바이더 시그니처에 인자를 더하면 룰 2 의 인터페이스가
> 호출 목적마다 갈라진다. `ContextVar` 는 asyncio 태스크별로 값이 분리되므로 동시에 도는
> 파이프라인끼리 서로의 스코프를 덮어쓰지 않는다.

> ### 왜 호출 즉시 쓰지 않고 스코프 종료에 모아 쓰는가
> 호출마다 세션을 열면 파이프라인이 이미 쥔 커넥션에 **더해** 한 개를 더 잡는다. 커넥션
> 풀이 병목인 상황(운영 전환 항목 A)에서 그 배수는 위험하다. 대신 `finally` 에서 flush 하므로
> **파이프라인이 예외로 죽어도 이미 쓴 토큰은 기록된다.** 프로세스가 통째로 죽는 경우만
> 유실되고, 그건 회계가 아니라 관측이라는 이 테이블의 성격상 허용한다.

⚠️ 스코프 밖에서 부른 호출은 **버린다**(로그만). 스크립트(`scripts/*.py`)가 프로바이더를
직접 쓰는 경로가 있고, 거기에 프로젝트 귀속을 강요하면 측정 도구가 못 돈다.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm import pricing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageScope:
    """이 태스크가 만드는 호출의 귀속 대상."""

    project_id: uuid.UUID
    user_id: uuid.UUID | None = None
    question_id: uuid.UUID | None = None


_scope: ContextVar[UsageScope | None] = ContextVar("llm_usage_scope", default=None)
_buffer: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_usage_buffer", default=None)


def record(
    *,
    step: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> None:
    """호출 1건을 버퍼에 담는다. 스코프가 없으면 조용히 버린다.

    ⚠️ 여기서 DB 를 건드리지 않는다 — 프로바이더는 동기 흐름 안에서 이걸 부르고,
    그 지점에서 IO 를 하면 LLM 호출 경로에 DB 지연이 얹힌다.
    """
    buffer = _buffer.get()
    if buffer is None:
        logger.debug("usage 스코프 밖 호출이라 기록하지 않는다: step=%s model=%s", step, model)
        return

    buffer.append(
        {
            "step": step,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cost_usd": pricing.cost_usd(
                model, input_tokens=input_tokens, output_tokens=output_tokens
            ),
        }
    )


@dataclass
class _Tokens:
    """`begin` 이 돌려주고 `flush` 가 되돌리는 ContextVar 토큰 쌍."""

    scope: Any
    buffer: Any


def begin() -> _Tokens:
    """수집을 시작한다. 귀속 대상은 아직 몰라도 된다 — `bind` 로 나중에 채운다.

    ⚠️ 파이프라인 진입점은 **질문을 읽기 전이라 project_id 를 모른다.** 그렇다고 질문을
    읽은 뒤에 스코프를 열면 그 앞의 호출을 놓치고, 큰 블록을 통째로 들여써야 한다.
    수집 시작과 귀속을 분리해 둘 다 피한다.
    """
    return _Tokens(scope=_scope.set(None), buffer=_buffer.set([]))


def bind(
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    question_id: uuid.UUID | None = None,
) -> None:
    """귀속 대상을 채운다. `begin` 뒤 아무 때나 부를 수 있고, 여러 번 불러도 마지막이 이긴다."""
    _scope.set(UsageScope(project_id, user_id, question_id))


async def flush(tokens: _Tokens) -> None:
    """모아 둔 것을 적재하고 ContextVar 를 되돌린다. **`finally` 에서 부른다.**

    귀속 대상이 끝내 안 채워졌으면(= 질문을 읽기도 전에 죽었으면) 버린다 — 어느 프로젝트의
    비용인지 모르는 행은 집계에 쓸 수 없고, 그런 경우 호출도 거의 없다.
    """
    rows = _buffer.get() or []
    scope = _scope.get()
    _buffer.reset(tokens.buffer)
    _scope.reset(tokens.scope)
    if not rows:
        return
    if scope is None:
        logger.warning("귀속 대상 없이 끝난 LLM 호출 %s건을 버린다", len(rows))
        return
    await _persist(scope, rows)


@asynccontextmanager
async def usage_scope(
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    question_id: uuid.UUID | None = None,
) -> AsyncIterator[None]:
    """귀속 대상을 처음부터 아는 경로용 편의 래퍼 (확정문 번역·교훈 추출 등).

    ⚠️ **중첩하지 마라.** 안쪽 스코프가 바깥 버퍼를 가리면 바깥 호출이 유실된다.
    """
    tokens = begin()
    bind(project_id=project_id, user_id=user_id, question_id=question_id)
    try:
        yield
    finally:
        await flush(tokens)


def _default_session_factory() -> "AsyncSession":
    from app.database import AsyncSessionLocal

    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 **유일한 지점**이다 (`ingest.session_factory` 와 같은 이유).
#    적재가 자체 세션을 여는 것이 설계인데, 테스트에서는 그 세션이 바깥 롤백 트랜잭션에
#    물려 있어야 아직 커밋되지 않은 프로젝트를 볼 수 있다 (`03 §5.3`).
session_factory: "Callable[[], AsyncSession]" = _default_session_factory


async def _persist(scope: UsageScope, rows: list[dict[str, Any]]) -> None:
    """**자체 세션·자체 커밋**이다.

    호출자의 트랜잭션에 얹으면 파이프라인이 롤백될 때 사용량까지 사라진다 — 토큰은 이미
    소비됐으므로 그건 사실과 다르다. 적재 자체가 실패해도 호출자를 죽이지 않는다:
    사용량 기록 실패가 답변 발행을 막으면 본말이 전도된다.
    """
    from app.models.llm_usage import LLMUsage

    try:
        async with session_factory() as db:
            db.add_all(
                [
                    LLMUsage(
                        project_id=scope.project_id,
                        user_id=scope.user_id,
                        question_id=scope.question_id,
                        **row,
                    )
                    for row in rows
                ]
            )
            await db.commit()
    except Exception:
        logger.exception(
            "llm_usage 적재 실패 (project=%s, %s건) — 호출자는 계속 진행한다",
            scope.project_id,
            len(rows),
        )
