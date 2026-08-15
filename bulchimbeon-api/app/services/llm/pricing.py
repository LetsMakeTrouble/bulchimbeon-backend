"""모델 단가표 — **앱 전체의 단일 원천** (`03 §4.2`).

⚠️ 종전에는 `scripts/measure_tokens.py` 와 `scripts/compare_models.py` 두 곳에 같은 표가
중복돼 있었다. 단가는 바뀌는 값이라 두 곳에 두면 조용히 갈린다. 여기 하나만 고친다.

> ### 비용은 **기록 시점 단가로 스냅샷**한다
> `llm_usage.cost_usd` 는 조회 시점에 다시 계산하지 않는다. 단가가 바뀌면 과거 사용분의
> 비용까지 소급 변경돼 "지난달에 얼마 썼나"의 답이 달라지기 때문이다. 회계는 그 시점의
> 사실을 보존해야 한다.

가격 단위는 **1M 토큰당 USD** 다. 모르는 모델은 0 으로 두고 로그로 드러낸다 —
조용히 추정한 값이 비용 통계에 섞이는 쪽이 더 위험하다.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# (입력, 출력) — 1M 토큰당 USD. 2026-08-09 기준.
PRICE_PER_MILLION: dict[str, tuple[str, str]] = {
    "gpt-5.6-sol": ("5.00", "30.00"),
    "gpt-5.6-terra": ("2.50", "15.00"),
    "gpt-5.6-luna": ("0.20", "1.20"),
    "text-embedding-3-small": ("0.02", "0.00"),
    # 기본 임베딩 모델 (`config.embedding_model`). small 의 6.5배지만 절대값이 작다 —
    # 문서 26청크 + 질문당 1~2회라 이력 109건을 돌려도 1센트 단위다.
    "text-embedding-3-large": ("0.13", "0.00"),
}

_MILLION = Decimal(1_000_000)


def cost_usd(model: str, *, input_tokens: int, output_tokens: int) -> Decimal:
    """USD 비용. 미등록 모델은 0 을 돌려주고 경고를 남긴다.

    ⚠️ `reasoning_tokens` 를 따로 더하지 않는다 — Responses API 의 `output_tokens` 에
    **이미 포함된** 값이라 더하면 이중 계상이다 (`openai_provider._record_usage`).
    """
    rates = PRICE_PER_MILLION.get(model)
    if rates is None:
        logger.warning("단가표에 없는 모델이라 비용을 0 으로 기록한다: %s", model)
        return Decimal(0)

    input_rate, output_rate = (Decimal(rate) for rate in rates)
    return (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / _MILLION
