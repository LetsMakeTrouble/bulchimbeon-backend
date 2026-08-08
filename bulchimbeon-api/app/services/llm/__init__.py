"""LLM 프로바이더 선택 (`03 §4` — `LLM_PROVIDER` env).

호출자는 **항상 `get_provider()` 를 거친다.** 구현 클래스를 직접 만들지 않는다 —
테스트가 프로바이더를 갈아끼우는 지점이 한 곳이어야 실 API 호출 금지(룰 2)를 강제할 수 있다.
"""

from app.config import settings
from app.services.llm.base import LLMProvider, LLMProviderError
from app.services.llm.fake_provider import FakeLLMProvider
from app.services.llm.openai_provider import OpenAIProvider

__all__ = ["FakeLLMProvider", "LLMProvider", "LLMProviderError", "OpenAIProvider", "get_provider"]

_PROVIDERS: dict[str, type] = {
    "openai": OpenAIProvider,
    "fake": FakeLLMProvider,
}

# 프로바이더 이름별 싱글턴. OpenAI 클라이언트는 커넥션 풀을 들고 있어 매번 만들면 낭비다.
_instances: dict[str, LLMProvider] = {}


def get_provider() -> LLMProvider:
    """`settings.llm_provider` 를 **호출 시점에** 읽는다.

    import 시점에 고정하면 테스트가 프로바이더를 바꿔도 이미 만들어진 인스턴스가 남는다.
    """
    name = settings.llm_provider
    provider_class = _PROVIDERS.get(name)
    if provider_class is None:
        raise LLMProviderError(f"알 수 없는 LLM_PROVIDER={name!r}. 가능한 값: {sorted(_PROVIDERS)}")
    if name not in _instances:
        _instances[name] = provider_class()
    return _instances[name]


def reset_provider_cache() -> None:
    """테스트에서 프로바이더를 갈아끼운 뒤 캐시를 비운다."""
    _instances.clear()
