"""LLM 구조화 출력 스키마 (`06 §2` ①②④⑤⑦).

> ### ⛔ strict Structured Outputs 규약 — 위반하면 **400** 이다 (`06 §2` ④, M-1 실측)
> 1. **모든 필드 required.** optional 은 `Optional[X]` 로 표현하고 필드를 빼지 않는다.
> 2. 모든 object 에 `additionalProperties: false` → `model_config = ConfigDict(extra="forbid")`.
> 3. **기본값이 있는 필드 금지.** `conflict_chunk_ids: list[str] = []` 가 400 의 **직접 원인**이다.
>    빈 배열도 모델이 항상 채우게 한다.
> 4. `minItems` / `uniqueItems` 같은 미지원 키워드는 스키마에 넣지 않는다 — 필요하면
>    **Pydantic validator 로 사후 검증**한다 (`field_validator` 는 JSON Schema 에 나타나지 않는다).
>
> 이 파일에서 `= ...` 형태의 기본값을 보면 그것이 곧 결함이다.
"""

from pydantic import BaseModel, ConfigDict, field_validator


class TranslationOut(BaseModel):
    """① 번역 + 긴급 제안 (`06 §2` ①).

    `suggest_urgent` 는 **제안일 뿐**이며 긴급 여부를 정하는 주체는 질문자다 (D10).
    """

    model_config = ConfigDict(extra="forbid")

    content_en: str
    suggest_urgent: bool


class SameQuestionOut(BaseModel):
    """② 재사용 2차 게이트 — "이 두 질문은 같은 질문인가?" (룰 4).

    단일 임계값에 재사용을 걸지 않기 위한 장치다. `yes` 일 때만 재사용한다.
    """

    model_config = ConfigDict(extra="forbid")

    same_question: bool


class Sentence(BaseModel):
    """④ 가 만드는 문장 하나.

    `text_ko` 를 함께 생성해 ⑧ 의 ko 번역 호출을 없앤다 → 🟢/🟡 경로가 4회 → **3회** (`06 §0`).
    """

    model_config = ConfigDict(extra="forbid")

    text_en: str
    text_ko: str

    # ⛔ 기본값 금지. 인용이 없으면 모델이 빈 배열을 명시적으로 채운다.
    #    id 는 프롬프트 지역 별칭(`ch-1`)이며 **UUID 를 모델에 노출하지 않는다** (`06 §2` ④).
    chunk_ids: list[str]


class SentencesOut(BaseModel):
    """④ 근거 기반 생성 결과 (`06 §2` ④)."""

    model_config = ConfigDict(extra="forbid")

    sentences: list[Sentence]
    not_answerable: bool
    conflict: bool

    # ⛔ 기본값 금지 (400 의 직접 원인).
    conflict_chunk_ids: list[str]

    @field_validator("conflict_chunk_ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        """`uniqueItems` 는 strict 스키마가 지원하지 않으므로 여기서 처리한다 (`06 §2` ④)."""
        return list(dict.fromkeys(value))


class SentenceVerdict(BaseModel):
    """⑤ 문장별 판정. `index` 는 ④ 결과의 1-based 순번이다."""

    model_config = ConfigDict(extra="forbid")

    index: int
    supported: bool

    # ⑤ 는 판정하려고 이미 근거 문장을 찾은 상태다. 그것을 돌려받아 `citations[].quote`
    # 로 저장한다 — 호출은 늘지 않고 화면 인용이 청크 앞 500자에서 실제 근거 문장이 된다.
    # ⛔ 기본값 금지. 근거가 없으면 모델이 빈 문자열을 명시적으로 채운다.
    #    지어낸 문장을 그대로 믿지 않는다 — 청크 실재 검증은 `answer._citation_quote`.
    quote: str


class VerdictsOut(BaseModel):
    """⑤ 근거 검증 결과 (`06 §2` ⑤).

    ⚠️ 이 호출은 **`LLM_MODEL_VERIFY`** 로 나간다. 생성과 검증이 같은 호출이면
    "매칭률 자기평가는 순환논리 아닌가"에 답할 수 없다 (환각 방어 2겹, `06 §7`).
    """

    model_config = ConfigDict(extra="forbid")

    verdicts: list[SentenceVerdict]


class QuestionStructOut(BaseModel):
    """⑦ 🔴 질문 구조화 — 배경 → 질문 → 선택지 (룰 8, `06 §2` ⑦).

    `answers.question_struct` 에 저장되고 M4 가 `review_cards.question_struct` 로 복사한다.
    """

    model_config = ConfigDict(extra="forbid")

    background: str
    question: str
    options: list[str]


class TextOut(BaseModel):
    """`translate()` 전용 — 단문 번역 결과."""

    model_config = ConfigDict(extra="forbid")

    text: str


class LessonOut(BaseModel):
    """교훈 추출 — 원답 vs 수정답의 차이에서 뽑은 **재사용 가능한 한 줄 원칙** (`06 §3`).

    필드가 하나뿐인 이유는 `06 §3` 의 출력 예시가 `{"lesson": "..."}` 하나이기 때문이다.
    설명·근거·확신도를 덧붙이면 `content_hash` 대조(D8)가 흔들린다 — 같은 원칙이라도
    부가 문장이 매번 달라지면 삭제한 교훈이 다른 해시로 되살아난다.
    """

    model_config = ConfigDict(extra="forbid")

    lesson: str
