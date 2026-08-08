"""OpenAI 프로바이더.

⚠️ **openai SDK 를 import 해도 되는 유일한 곳이 이 패키지다** (룰 2).

M2 범위는 **임베딩만 실구현**이다. `complete_json` · `translate` 는 M3 질문 파이프라인에서
`responses.parse` 로 구현된다 (`06 §0` — `chat.completions` 와 섞지 않는다).

⛔ **금지 파라미터**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed`
— GPT-5 계열에 전달하면 400 이다 (`06 §6`, M-1 실측). `max_tokens` 대신
`max_completion_tokens` 를 쓴다. 이 규칙은 M3 구현 시점에 화이트리스트로 강제한다.
"""

from typing import Any

from openai import AsyncOpenAI

from app.config import EMBEDDING_DIM_FIXED, settings
from app.services.llm.base import LLMProviderError


class OpenAIProvider:
    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise LLMProviderError(
                "OPENAI_API_KEY 가 비어 있다. `.env` 를 로드했는지 확인하라 "
                "(`set -a; . ./.env; set +a`). 테스트는 LLM_PROVIDER=fake 를 쓴다."
            )
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """`06 §1` ③ — 인제스트 임베딩.

        `dimensions` 를 명시해 응답 차원을 컬럼(`vector(1536)`)에 맞춘다. 그럼에도 응답을
        다시 검증한다 — 모델 교체로 차원이 달라지면 INSERT 시점이 아니라 **여기서** 죽어야
        원인이 보인다 (`04` 상단 fail-fast).
        """
        if not texts:
            return []

        try:
            response = await self._client.embeddings.create(
                model=settings.embedding_model,
                input=texts,
                dimensions=settings.embedding_dim,
            )
        except Exception as exc:  # SDK 예외를 호출자에게 그대로 흘리지 않는다.
            raise LLMProviderError(f"embedding 호출 실패: {exc}") from exc

        # 응답 순서 보장은 index 필드로만 확실해진다.
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]

        if len(vectors) != len(texts):
            raise LLMProviderError(f"임베딩 개수 불일치: 요청 {len(texts)} / 응답 {len(vectors)}")
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM_FIXED:
                raise LLMProviderError(
                    f"임베딩 차원 불일치: 컬럼은 vector({EMBEDDING_DIM_FIXED}) 고정인데 "
                    f"응답은 {len(vector)} 이다."
                )
        return vectors

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        # TODO(M3): responses.parse + strict Structured Outputs (`06 §2` ④·⑤·⑦).
        #   strict 모드는 **기본값 있는 필드를 금지**한다 — 전 필드를 required 로 두고
        #   빈 배열도 모델이 명시적으로 채우게 한다 (M-1 실측: 400 의 직접 원인).
        raise NotImplementedError("complete_json 은 M3 질문 파이프라인에서 구현된다 (`06 §2`).")

    async def translate(self, text: str, source: str, target: str) -> str:
        # TODO(M3): 번역 + 긴급 제안 (`06 §2` ①).
        raise NotImplementedError("translate 는 M3 질문 파이프라인에서 구현된다 (`06 §2`).")
