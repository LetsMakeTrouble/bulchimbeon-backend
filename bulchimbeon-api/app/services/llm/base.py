"""LLM 프로바이더 인터페이스 (`03 §3`, `CLAUDE.md` 룰 2).

**LLM 호출은 이 프로토콜을 구현한 프로바이더 경유만 허용된다.**
라우터·서비스에서 openai SDK 를 직접 import 하지 않는다 — "+@" 확장(다른 프로바이더)은
구현 클래스를 하나 더 추가하는 것으로 끝나야 한다.

⛔ **금지 파라미터**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed`
— GPT-5 계열에 전달하면 **400** 이다 (`06 §6`, M-1 실측). 호출자가 실수로 넘겨도 새어 나가지
않도록 **프로바이더 계층에서 화이트리스트로 드롭**한다. `FORBIDDEN_PARAMS` 가 그 정본이다.
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

# `06 §2` ④ — 재시도 최대 2회(= 총 3회 시도). 소진하면 강제 🔴 `schema_failed` 다.
MAX_SCHEMA_RETRIES = 2

# `06 §6` — 이 키들은 어떤 경로로도 SDK 에 전달되지 않는다.
FORBIDDEN_PARAMS = frozenset(
    {"temperature", "top_p", "presence_penalty", "frequency_penalty", "seed"}
)


class LLMProviderError(RuntimeError):
    """프로바이더 계층에서 올라오는 실패. 호출자는 SDK 예외를 직접 보지 않는다."""


class LLMSchemaError(LLMProviderError):
    """구조화 출력 검증이 **재시도를 소진하고도** 실패했다 (`06 §2` ④).

    파이프라인은 이것만 잡아 강제 🔴 `schema_failed` 로 떨어뜨린다. 네트워크·타임아웃 실패는
    `LLMProviderError` 로 남아 파이프라인 총 실패(D23)로 간다 — 둘을 뭉치면 장애가
    "근거 부족"으로 위장된다.
    """


def sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """금지 파라미터를 드롭한다 (`06 §6`).

    화이트리스트 방식(허용 키만 통과)이 아니라 블랙리스트인 이유는, 모델별로 새로 생기는
    정상 파라미터까지 막으면 프로바이더가 확장을 가로막기 때문이다. 400 을 내는 다섯 개만
    확정적으로 제거한다.
    """
    return {key: value for key, value in params.items() if key not in FORBIDDEN_PARAMS}


@runtime_checkable
class LLMProvider(Protocol):
    """`03 §3` 의 세 가지 능력. 단계별 사용처는 `06 §2`."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 배치를 임베딩한다. 응답 차원은 `EMBEDDING_DIM` 으로 검증된다 (`04` 상단)."""
        ...

    async def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Structured Outputs strict 호출 (`06 §2` ①②④⑤⑦).

        `schema` 는 **Pydantic 모델 클래스**다 — `responses.parse(text_format=...)` 가 그것을
        그대로 받고, 검증도 같은 클래스로 한다. 검증 실패는 `MAX_SCHEMA_RETRIES` 만큼
        재시도하고 소진하면 `LLMSchemaError` 다.

        `model` 로 호출 모델을 바꾼다 — ⑤ 근거 검증이 `LLM_MODEL_VERIFY` 를 쓰는 근거다.
        """
        ...

    async def translate(self, text: str, source: str, target: str) -> str:
        """단문 번역 (`02 §8` — 담당자 수정문의 en→ko 등)."""
        ...
