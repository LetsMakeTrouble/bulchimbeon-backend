"""OpenAI 프로바이더.

⚠️ **openai SDK 를 import 해도 되는 유일한 곳이 이 패키지다** (룰 2).

> ### 호출 방식은 하나로 고정한다 (`06 §0`)
> **`client.responses.parse(model=…, input=…, text_format=Model)`** 만 쓴다.
> `chat.completions` 와 섞지 않는다 — M-1 실측(2026-08-07, n=8)에서 `chat` 은 중앙값이
> 빠르지만 σ=4.24 로 튀어(최대 17.31s) **p90 기준 🟢/🟡 경로 25초를 지키지 못했다**(32.7s).
> 데드라인 설계는 중앙값이 아니라 p90 으로 한다.

⛔ **금지 파라미터**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed`
— GPT-5 계열에 전달하면 400 이다 (`06 §6`). `base.sanitize_params` 가 이를 강제한다.
"""

import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import EMBEDDING_DIM_FIXED, settings
from app.services.llm.base import (
    MAX_SCHEMA_RETRIES,
    LLMProviderError,
    LLMSchemaError,
    sanitize_params,
)

logger = logging.getLogger(__name__)

# Responses API 의 출력 상한 파라미터 이름은 **`max_output_tokens`** 다.
# `06 §6` 의 요지는 "`max_tokens` 를 쓰지 말 것"이며, `max_completion_tokens` 는
# Chat Completions 를 쓸 때의 이름이다 — 우리는 `responses.parse` 로 고정했으므로 이쪽이다.
# 추론 토큰도 이 한도에 포함되므로 여유 있게 잡는다(부족하면 `incomplete` → schema_failed 가 된다).
MAX_OUTPUT_TOKENS = 8192


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

    async def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """strict Structured Outputs (`06 §2` ①②④⑤⑦).

        재시도는 **스키마 실패에만** 쓴다. 네트워크·타임아웃 실패는 `LLMProviderError` 로
        올려 파이프라인 총 실패(D23)로 보낸다 — 인프라 장애를 `schema_failed` 로 기록하면
        환각 방어 3겹의 증적이 오염된다.

        ⚠️ strict 모드에서는 스키마 위반이 사실상 발생하지 않는다. 실 API 에서 이 경로에
        도달하는 것은 **refusal** 과 **max token 초과**뿐이다 (`06 §2` ④).
        """
        params = sanitize_params(
            {
                "model": model or settings.llm_model_answer,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "text_format": schema,
                "reasoning": {"effort": settings.llm_reasoning_effort},
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            }
        )

        last_reason = "unknown"
        for attempt in range(1 + MAX_SCHEMA_RETRIES):
            try:
                response = await self._client.responses.parse(**params)
            except ValidationError as exc:  # 모델 출력이 스키마를 벗어났다.
                last_reason = f"validation: {exc}"
                logger.warning(
                    "complete_json 스키마 실패 (%s/%s)", attempt + 1, 1 + MAX_SCHEMA_RETRIES
                )
                continue
            except Exception as exc:
                raise LLMProviderError(f"responses.parse 호출 실패: {exc}") from exc

            parsed = response.output_parsed
            if parsed is None:
                # refusal 또는 `incomplete`(max token 초과). 둘 다 스키마 실패로 센다.
                last_reason = f"status={getattr(response, 'status', None)} parsed=None"
                logger.warning("complete_json 이 파싱 결과를 주지 않았다: %s", last_reason)
                continue

            return parsed.model_dump()

        raise LLMSchemaError(
            f"{schema.__name__} 구조화 출력이 {1 + MAX_SCHEMA_RETRIES}회 모두 실패했다 "
            f"({last_reason})."
        )

    async def translate(self, text: str, source: str, target: str) -> str:
        """단문 번역 (`02 §8`).

        ⚠️ 파이프라인 ⑧ 은 이것을 부르지 않는다 — ④ 가 문장별 `text_ko` 를 함께 만들었기
        때문이다 (`06 §0` 4회 → 3회 최적화). 여기는 담당자 수정문의 en→ko 같은 경로용이다.
        """
        from app.services.pipeline.llm_schemas import TextOut

        result = await self.complete_json(
            system=(
                f"Translate the user's text from {source} to {target}. "
                "Return only the translation, preserving meaning and tone. Do not add commentary."
            ),
            user=text,
            schema=TextOut,
            # ⚠️ `llm_model_translate`(① 질문 번역)가 아니다 — 이 경로는 담당자 확정문
            # en→ko 이고, 그 결과가 **재번역 금지된 확정 원문**으로 굳는다 (룰 4).
            model=settings.llm_model_answer_translate,
        )
        return str(result["text"])
