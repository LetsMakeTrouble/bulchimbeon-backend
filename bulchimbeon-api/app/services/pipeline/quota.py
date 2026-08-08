"""일일 LLM 호출 상한 (`06 §6`, 룰 1).

`settings.daily_llm_call_limit`(기본 500)을 넘으면 질문 접수는 받되 **강제 🔴
`quota_exceeded`** 로 떨어뜨린다. ⚠️ 이 값은 env 가 아니라 **`projects.settings`** 다 —
임계값 하드코딩 금지 원칙의 일관성 때문이다 (`04 §3`).

> ### 왜 테이블이 아니라 프로세스 메모리인가
> `04 §2` 에 호출 카운터 테이블이 없고, 계약서에 없는 테이블을 임의로 만들지 않는다.
> 이 앱은 **`--workers 1` 고정**(룰 9)이 아키텍처 전제이므로 프로세스 하나가 모든 호출을
> 본다. 대신 **재시작하면 카운터가 0으로 돌아간다** — 상한의 성격이 "폭주 방어"이지
> "회계"가 아니므로 데모 규모에서 이 손실은 허용된다.
> 워커를 늘리거나 정확한 회계가 필요해지는 시점의 정답은 DB 카운터(또는 Redis)다.
"""

from collections import defaultdict
from datetime import UTC, date, datetime
from uuid import UUID

# {(project_id, UTC 날짜): 호출 수}
_counters: dict[tuple[UUID, date], int] = defaultdict(int)


def _today() -> date:
    return datetime.now(UTC).date()


def used(project_id: UUID) -> int:
    """오늘(UTC) 이 프로젝트가 쓴 LLM 호출 수."""
    return _counters[(project_id, _today())]


def consume(project_id: UUID, count: int = 1) -> int:
    """호출을 기록하고 누적치를 돌려준다. **실제 호출 직전에** 부른다."""
    key = (project_id, _today())
    _counters[key] += count
    return _counters[key]


def is_exceeded(project_id: UUID, limit: int) -> bool:
    """상한 초과 여부. `limit` 은 `projects.settings.daily_llm_call_limit` 이다."""
    return used(project_id) >= limit


def set_used(project_id: UUID, count: int) -> None:
    """테스트가 `quota_exceeded` 분기를 재현하기 위한 진입점."""
    _counters[(project_id, _today())] = count


def reset() -> None:
    """테스트 격리용. 운영 코드에서 부르지 않는다."""
    _counters.clear()
