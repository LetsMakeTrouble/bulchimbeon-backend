"""LLM 프로바이더 인터페이스 (`03 §3`, `CLAUDE.md` 룰 2).

**LLM 호출은 이 프로토콜을 구현한 프로바이더 경유만 허용된다.**
라우터·서비스에서 openai SDK 를 직접 import 하지 않는다 — "+@" 확장(다른 프로바이더)은
구현 클래스를 하나 더 추가하는 것으로 끝나야 한다.
"""

from typing import Any, Protocol, runtime_checkable


class LLMProviderError(RuntimeError):
    """프로바이더 계층에서 올라오는 실패. 호출자는 SDK 예외를 직접 보지 않는다."""


@runtime_checkable
class LLMProvider(Protocol):
    """`03 §3` 의 세 가지 능력. 단계별 사용처는 `06 §2`."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 배치를 임베딩한다. 응답 차원은 `EMBEDDING_DIM` 으로 검증된다 (`04` 상단)."""
        ...

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Structured Outputs strict 호출 (`06 §2` ④·⑤·⑦). M3 에서 실구현된다."""
        ...

    async def translate(self, text: str, source: str, target: str) -> str:
        """번역 (`06 §2` ①). M3 에서 실구현된다."""
        ...
