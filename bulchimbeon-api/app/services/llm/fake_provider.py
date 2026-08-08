"""FakeLLMProvider — 네트워크 없이 도는 결정적 프로바이더 (`06 §5`).

테스트는 **반드시** 이 프로바이더를 쓴다. 실 API 를 호출하는 테스트는 금지다 (룰 2).

> ### ⚠️ 한계 (`06 §5`)
> 임베딩은 **텍스트 해시 기반 결정적 벡터**다. 실제 의미 유사도를 반영하지 않으므로
> 이 벡터들 사이의 코사인 값은 **무의미하다.** 파이프라인의 분기 로직은 검증되지만
> 캘리브레이션 결함은 원리적으로 검출되지 않는다 — 그 역할은 M-1 게이트가 담당한다.
"""

import hashlib
import math
import random
from typing import Any

from app.config import EMBEDDING_DIM_FIXED
from app.services.llm.base import LLMProviderError


def deterministic_embedding(text: str, dim: int = EMBEDDING_DIM_FIXED) -> list[float]:
    """같은 텍스트 → 항상 같은 단위 벡터.

    코사인 유사도가 `1 - (embedding <=> :q)` 로 계산되므로 단위 벡터로 정규화해 둔다.
    (정규화하지 않으면 길이가 유사도에 섞여 들어가 분기 테스트가 흔들린다.)
    """
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    vector = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:  # 확률적으로 불가능하지만 0 나눗셈은 막는다.
        return [0.0] * (dim - 1) + [1.0]
    return [value / norm for value in vector]


class FakeLLMProvider:
    """입력에 대해 결정적 출력을 돌려준다. 호출 이력은 테스트가 단언할 수 있게 남긴다."""

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []
        self.complete_json_calls: list[tuple[str, str]] = []
        self.translate_calls: list[tuple[str, str, str]] = []
        # 테스트가 특정 텍스트에서 임베딩을 실패시켜 ingest failed 경로를 밟게 한다.
        self.embed_failure: str | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        if self.embed_failure is not None and any(self.embed_failure in text for text in texts):
            raise LLMProviderError(f"fake embed failure: {self.embed_failure}")
        return [deterministic_embedding(text) for text in texts]

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """스키마의 필수 키를 결정적으로 채운다.

        M2 는 이 경로를 쓰지 않는다(인제스트는 임베딩만 한다). 마커 기반 분기 응답은
        M3 파이프라인 테스트가 요구하는 형태로 그때 확장한다 (`06 §5`).
        """
        self.complete_json_calls.append((system, user))
        digest = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", list(properties))
        return {key: _fake_value(properties.get(key, {}), digest) for key in required}

    async def translate(self, text: str, source: str, target: str) -> str:
        self.translate_calls.append((text, source, target))
        return f"[{target}] {text}"


def _fake_value(spec: dict[str, Any], digest: str) -> Any:
    json_type = spec.get("type", "string")
    if json_type == "boolean":
        return int(digest[:2], 16) % 2 == 0
    if json_type == "integer":
        return int(digest[:2], 16)
    if json_type == "number":
        return int(digest[:2], 16) / 255
    if json_type == "array":
        return []
    if json_type == "object":
        return {}
    return f"fake:{digest[:12]}"
