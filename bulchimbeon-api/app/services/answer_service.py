"""답변 상태 가드 (`04 §6`, D12·D13).

M3 은 답변을 **만들기만** 하고 확정·피드백 API 는 M4 가 붙인다. 그럼에도 상태 가드를
지금 두는 이유는, `expired` 가 **종착 상태**라는 사실이 파이프라인 쪽 규약(만료 스위퍼·
재사용 풀)과 한 몸이기 때문이다 — 판정을 M4 로 미루면 두 곳에 흩어진다.
"""

from app.core.errors import FeedbackNotAllowed, InvalidCardAction
from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_EXPIRED,
    ANSWER_STATE_VERIFIED,
    Answer,
)

# `02` 룰 3 / D12 — 피드백은 `draft` · `verified` 에서만 받는다.
FEEDBACK_ALLOWED_STATES = (ANSWER_STATE_DRAFT, ANSWER_STATE_VERIFIED)


def ensure_confirmable(answer: Answer) -> None:
    """확정(승인·수정·원안유지) 가능 여부 (D13).

    **만료는 "확정 불가 + 상태 표기"다.** 승인·수정 대상에서 제외되며, 만료 답변의 카드를
    처리하려 하면 409 다. 스위퍼가 살아 있는 카드 밑에서 답변을 죽이지 않으므로(D14)
    이 경로에 도달하는 것은 "카드 없이 만료된 답변"뿐이다.

    ⚠️ 코드 선택 근거: `05 §1.4` 의 409 목록에 만료 전용 코드가 없다. 계약서에 없는 코드를
    새로 만들지 않는다는 규약(`05` 는 프론트와의 계약)에 따라, 의미가 가장 가까운
    `INVALID_CARD_ACTION`("이 카드에 유효하지 않은 액션")을 쓴다.
    """
    if answer.state == ANSWER_STATE_EXPIRED:
        raise InvalidCardAction("만료된 답변은 확정할 수 없습니다.")


def ensure_feedback_allowed(answer: Answer) -> None:
    """피드백 허용 상태 검사 (D12).

    `expired` · `rejected` · `under_review` 는 409 `FEEDBACK_NOT_ALLOWED` 다.
    """
    if answer.state not in FEEDBACK_ALLOWED_STATES:
        raise FeedbackNotAllowed()
