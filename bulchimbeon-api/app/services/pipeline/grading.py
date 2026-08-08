"""S · G · 매칭률 · 등급 (`02` 룰 1, `06 §2` ③⑤⑥).

> ### 이 파일이 지키는 세 가지 (하나라도 어기면 등급 체계가 무너진다)
>
> 1. **`sim_raw` 는 유사도이지 거리가 아니다.** pgvector 의 `<=>` 는 거리이므로
>    `1 - (embedding <=> :q)` 로 만들어 넘어온다 (`retrieval.py`). 여기서는 이미 유사도다.
> 2. **G 의 분모는 "생성 시점 원본 문장 수"로 고정**이다. 무근거 문장을 제거해도 분모는
>    줄지 않는다 — 분모를 프루닝 후 개수로 쓰면 **환각이 심할수록 등급이 올라가는**
>    비단조 함수가 된다 (`06 §2` ⑤).
> 3. **매칭률은 `min(S, G)`** 이지 평균이 아니다. 문서를 잘 찾아도 답이 문서를 벗어났으면 낮게.
>
> 임계값은 전부 인자로 받는다. 이 파일에 숫자를 박지 않는다 (룰 3) —
> 기본값의 유일한 정의 위치는 `config.DEFAULT_SETTINGS` 이고 런타임 원천은 `projects.settings` 다.
"""

from app.models.question import GRADE_GREEN, GRADE_RED, GRADE_YELLOW

# `06 §2` ⑤ — 비율 통계가 의미를 갖지 못하는 구간. 1~2문장은 `grounding_min` 을 적용하지 않고
# **전 문장 supported** 를 요구한다.
MIN_SENTENCES_FOR_RATIO = 3


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def search_score(sim_raw: float, *, s_floor: float, s_ceil: float) -> int:
    """S = round(clamp((sim_raw - s_floor) / (s_ceil - s_floor), 0, 1) × 100) (룰 1).

    임베딩 모델마다 코사인 분포 중심이 다르므로(`text-embedding-3-small` 은 평균 ≈0.43)
    원시값을 그대로 100배 하면 🟢 에 영원히 도달하지 못한다. 리스케일이 그 스케일 차이를
    흡수하므로 **80/50 등급 임계값은 모델이 바뀌어도 그대로 유지된다.**
    """
    span = s_ceil - s_floor
    if span <= 0:
        # 설정이 망가진 경우. 0 나눗셈 대신 하한 판정으로 떨어뜨린다.
        return 100 if sim_raw >= s_ceil else 0
    return round(clamp((sim_raw - s_floor) / span, 0.0, 1.0) * 100)


def grounding_score(supported_count: int, original_sentence_count: int) -> int:
    """G = round(지지 문장 수 / **생성 시점 원본 문장 수** × 100) (룰 1).

    ⛔ 두 번째 인자에 "프루닝 후 개수"를 넣지 말 것. 그러면 프루닝이 항상 `G=100` 을 만들어
    `min(S, G)` 의 G 가 죽은 코드가 되고, 환각 60%짜리 답변이 🟢 로 올라간다 (`06 §2` ⑤).
    """
    if original_sentence_count <= 0:
        return 0
    return round(supported_count / original_sentence_count * 100)


def matching_rate(s: int, g: int) -> int:
    """매칭률 = **min(S, G)** — 평균이 아니다 (룰 1)."""
    return min(s, g)


def grade_for(rate: int, *, green_threshold: int, yellow_threshold: int) -> str:
    """`≥ green` → 🟢 / `≥ yellow` → 🟡 / 미만 → 🔴 (`06 §2` ⑥)."""
    if rate >= green_threshold:
        return GRADE_GREEN
    if rate >= yellow_threshold:
        return GRADE_YELLOW
    return GRADE_RED


def requires_all_supported(sentence_count: int) -> bool:
    """1~2문장 답변은 **전 문장 supported 필수**다 (룰 1, `06 §2` ⑤).

    비율 통계가 의미를 갖지 못하는 구간이라 `grounding_min` 을 적용하지 않는다 —
    하나라도 무근거면 🔴 이다.
    """
    return 0 < sentence_count < MIN_SENTENCES_FOR_RATIO


def should_prune(g_raw: int, sentence_count: int, *, grounding_min: int) -> bool:
    """프루닝 조건 (`02` 룰 1).

    프루닝은 **발행 품질을 위한 조치일 뿐 매칭률과 무관**하다 — 분모가 고정이므로
    지우고 나서 다시 계산해도 G 는 그대로다.
    """
    return sentence_count >= MIN_SENTENCES_FOR_RATIO and g_raw < grounding_min
