"""FakeLLMProvider — 네트워크 없이 도는 결정적 프로바이더 (`06 §5`).

테스트는 **반드시** 이 프로바이더를 쓴다. 실 API 를 호출하는 테스트는 금지다 (룰 2).

## 마커 규약

질문 본문에 `[[fake:key=value,key2]]` 를 넣으면 파이프라인 전 단계가 그 지시를 따른다.
① 번역이 `content_en = "[en] {content_ko}"` 를 만들기 때문에 마커는 ②④⑦ 프롬프트까지
그대로 실려 간다 — 단계마다 따로 심을 필요가 없다.

| 마커 | 효과 | 대응 테스트 (`06 §5`) |
| --- | --- | --- |
| `sentences=N` | ④ 가 N 문장을 만든다 (기본 2) | 1·2 |
| `supported=K` | 앞의 K 문장만 ⑤ 에서 supported (기본 = N) | 1·2 |
| `ghost_citation` | 마지막 문장이 EVIDENCE 에 없는 별칭을 인용한다 | 인용 id 실재 검증 |
| `conflict` | ④ 가 `conflict=true` | 3 |
| `not_answerable` | ④ 가 `not_answerable=true` | 강제 🔴 `no_evidence` |
| `schema_fail` | ④ 가 3회 모두 스키마 위반 | 6 |
| `same_question=no` | ② 2차 게이트가 `no` (기본 `yes`) | 4 |
| `urgent` | ① 이 `suggest_urgent=true` | D10 |

⑤ 의 판정은 **문장 텍스트**로 결정된다(`Unsupported` 로 시작하는 문장이 unsupported).
⑤ 프롬프트에는 문장 원문이 실리므로 마커를 다시 주입하지 않아도 결정적이다.

> ### ⚠️ 한계 (`06 §5`)
> 임베딩은 **텍스트 해시 기반 결정적 벡터**다. 실제 의미 유사도를 반영하지 않으므로
> 이 벡터들 사이의 코사인 값은 **무의미하다.** 파이프라인의 분기 로직은 검증되지만
> 캘리브레이션 결함은 원리적으로 검출되지 않는다 — 그 역할은 M-1 게이트가 담당한다.
> 유사도에 의존하는 테스트는 `tests/pipeline_helpers.py` 가 목표 코사인을 갖는 벡터를
> **직접 만들어** 심는다.
"""

import hashlib
import math
import random
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import EMBEDDING_DIM_FIXED
from app.services.llm.base import MAX_SCHEMA_RETRIES, LLMProviderError, LLMSchemaError

_MARKER_RE = re.compile(r"\[\[fake:([^\]]*)\]\]")
_ALIAS_RE = re.compile(r"\bch-\d+\b")

# ⑤ 프롬프트의 문장 블록 머리말. answer.py 와 이 파일이 공유하는 유일한 포맷 계약이다.
SENTENCE_BLOCK_PREFIX = "[SENTENCE "

# ④ 가 만드는 문장의 접두사. ⑤ 가 이것만 보고 supported 를 결정한다.
SUPPORTED_PREFIX = "Supported"
UNSUPPORTED_PREFIX = "Unsupported"


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


def parse_markers(text: str) -> dict[str, str]:
    """`[[fake:a=1,b]]` → `{"a": "1", "b": "true"}`. 여러 번 나오면 나중 것이 이긴다."""
    markers: dict[str, str] = {}
    for group in _MARKER_RE.findall(text):
        for token in group.split(","):
            key, _, value = token.partition("=")
            key = key.strip()
            if key:
                markers[key] = value.strip() or "true"
    return markers


class FakeLLMProvider:
    """입력에 대해 결정적 출력을 돌려준다. 호출 이력은 테스트가 단언할 수 있게 남긴다."""

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []
        self.complete_json_calls: list[tuple[str, str, str, str | None]] = []
        self.translate_calls: list[tuple[str, str, str]] = []
        # 테스트가 특정 텍스트에서 임베딩을 실패시켜 ingest failed 경로를 밟게 한다.
        self.embed_failure: str | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        if self.embed_failure is not None and any(self.embed_failure in text for text in texts):
            raise LLMProviderError(f"fake embed failure: {self.embed_failure}")
        return [deterministic_embedding(text) for text in texts]

    async def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """실 프로바이더와 **같은 재시도 구조**를 따른다 (`06 §2` ④).

        마커가 스키마 위반을 지시하면 3회 모두 위반한 뒤 `LLMSchemaError` 를 던진다 —
        테스트 6 이 검증하는 것이 바로 이 소진 경로다.
        """
        self.complete_json_calls.append((schema.__name__, system, user, model))

        last_error: Exception | None = None
        for _ in range(1 + MAX_SCHEMA_RETRIES):
            raw = self._raw_payload(schema, user)
            try:
                return schema.model_validate(raw).model_dump()
            except ValidationError as exc:
                last_error = exc

        raise LLMSchemaError(f"{schema.__name__} fake 스키마 실패 (재시도 소진): {last_error}")

    async def translate(self, text: str, source: str, target: str) -> str:
        self.translate_calls.append((text, source, target))
        return f"[{target}] {text}"

    # --- 단계별 결정적 출력 -------------------------------------------------------------

    def _raw_payload(self, schema: type[BaseModel], user: str) -> Any:
        markers = parse_markers(user)
        name = schema.__name__

        if name == "TranslationOut":
            # ① 의 user 프롬프트는 **한국어 원문 그대로**다 (answer.py `_translate`).
            return {
                "content_en": f"[en] {user.strip()}",
                "suggest_urgent": "urgent" in markers,
            }

        if name == "SameQuestionOut":
            return {"same_question": markers.get("same_question", "yes") == "yes"}

        if name == "SentencesOut":
            return self._sentences_payload(markers, user)

        if name == "VerdictsOut":
            return self._verdicts_payload(user)

        if name == "QuestionStructOut":
            return {
                "background": "Fake background derived from the retrieved evidence.",
                "question": "Fake structured question for the answerer.",
                "options": ["Option A", "Option B"],
            }

        if name == "TextOut":
            return {"text": f"[translated] {user.strip()}"}

        raise LLMProviderError(f"FakeLLMProvider 가 모르는 스키마다: {name}")

    def _sentences_payload(self, markers: dict[str, str], user: str) -> Any:
        if "schema_fail" in markers:
            # 필수 필드를 빼서 3회 모두 검증에 실패시킨다 (테스트 6 — FakeLLM 전용 경로).
            return {"sentences": []}

        aliases = sorted(set(_ALIAS_RE.findall(user)), key=lambda alias: int(alias.split("-")[1]))
        primary = aliases[0] if aliases else "ch-1"

        if "not_answerable" in markers:
            return {
                "sentences": [],
                "not_answerable": True,
                "conflict": False,
                "conflict_chunk_ids": [],
            }

        total = max(1, int(markers.get("sentences", "2")))
        supported = min(total, max(0, int(markers.get("supported", str(total)))))

        sentences: list[dict[str, Any]] = []
        for index in range(1, total + 1):
            is_supported = index <= supported
            prefix = SUPPORTED_PREFIX if is_supported else UNSUPPORTED_PREFIX
            sentences.append(
                {
                    "text_en": f"{prefix} sentence {index}.",
                    "text_ko": f"{'근거 있는' if is_supported else '근거 없는'} 문장 {index}.",
                    "chunk_ids": [primary],
                }
            )

        if "ghost_citation" in markers:
            # EVIDENCE 에 없는 별칭 — 서버가 제거하고 그 문장을 supported=false 로 만든다.
            sentences[-1]["chunk_ids"] = ["ch-999"]

        conflict = "conflict" in markers
        return {
            "sentences": sentences,
            "not_answerable": False,
            "conflict": conflict,
            "conflict_chunk_ids": aliases[:2] if conflict else [],
        }

    def _verdicts_payload(self, user: str) -> Any:
        """⑤ — 프롬프트에 실린 문장 블록을 세어 판정한다.

        `Unsupported` 로 시작하는 문장만 `supported=false` 다. 인용이 비어 ④ 에서 이미
        제외된 문장은 애초에 이 프롬프트에 들어오지 않는다 (`06 §2` ⑤ "자동 false").
        """
        blocks = user.split(SENTENCE_BLOCK_PREFIX)[1:]
        return {
            "verdicts": [
                {"index": index, "supported": UNSUPPORTED_PREFIX not in block}
                for index, block in enumerate(blocks, start=1)
            ]
        }
