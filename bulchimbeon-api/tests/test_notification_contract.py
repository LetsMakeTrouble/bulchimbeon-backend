"""알림 타입·SSE 이벤트 어휘가 계약서와 1:1 인지 (`04 §4`, `05 §11`·`§12.3`).

⚠️ 기대값을 여기에 다시 적지 않는다 — `tests/test_config.py` 와 같은 이유다. 어휘를 테스트에
복사해 두면 그게 두 번째 원천이 되어, 계약서에서 이름이 바뀌어도 아무도 알아채지 못한다.
대신 문서를 파싱해 비교한다.

**계약에 없는 알림 타입·이벤트명을 새로 만들지 않는다**는 규약을 지키는 관문이 이 파일이다.
"""

import re

from app.config import PROJECT_ROOT
from app.models.notification import NOTIFICATION_TYPES
from app.schemas.notification import NotificationType
from app.services import sse_manager

API_CONTRACT_DOC = PROJECT_ROOT / "docs" / "05-api-contract.md"
DATA_MODEL_DOC = PROJECT_ROOT / "docs" / "04-data-model.md"


def _notification_types_from_data_model() -> list[str]:
    """`04 §4` 알림 타입 표의 첫 열 — **정본**이다 (`05 §11` 이 그것을 옮긴 것)."""
    doc = DATA_MODEL_DOC.read_text(encoding="utf-8")
    section = re.search(r"## 4\. 알림 타입\n(.*?)\n## 5\.", doc, re.S)
    assert section is not None, f"{DATA_MODEL_DOC} 에서 §4 알림 타입 표를 찾지 못했다"

    types: list[str] = []
    for row in section.group(1).splitlines():
        cells = [cell.strip() for cell in row.split("|")]
        if len(cells) < 3 or not cells[1].startswith("`"):
            continue
        # `sync.completed` / `sync.failed` 처럼 한 행에 둘이 들어 있는 경우가 있다.
        types.extend(re.findall(r"`([a-z_]+\.[a-z_]+)`", cells[1]))
    assert types, "알림 타입을 하나도 읽지 못했다"
    return types


def _sse_events_from_contract() -> list[str]:
    """`05 §12.3` 이벤트 목록 표의 첫 열."""
    doc = API_CONTRACT_DOC.read_text(encoding="utf-8")
    section = re.search(r"### 12\.3 이벤트 목록\n(.*?)\n---", doc, re.S)
    assert section is not None, f"{API_CONTRACT_DOC} 에서 §12.3 표를 찾지 못했다"

    events: list[str] = []
    for row in section.group(1).splitlines():
        cells = [cell.strip() for cell in row.split("|")]
        if len(cells) < 3 or not cells[1].startswith("`"):
            continue
        events.extend(re.findall(r"`([a-z_]+\.[a-z_]+)`", cells[1]))
    assert events, "이벤트를 하나도 읽지 못했다"
    return events


def test_notification_types_match_the_data_model() -> None:
    """`04 §4` 가 정본이다 — 12종."""
    assert set(_notification_types_from_data_model()) == set(NOTIFICATION_TYPES)
    assert len(NOTIFICATION_TYPES) == 12


def test_notification_schema_literal_matches_the_model() -> None:
    """스키마의 `Literal` 과 모델의 CHECK 대상이 같은 집합이어야 한다.

    갈라지면 DB 는 받아들이는데 응답 직렬화가 500 으로 죽는다(또는 그 반대).
    """
    assert set(NotificationType.__args__) == set(NOTIFICATION_TYPES)


def test_sse_events_match_the_api_contract() -> None:
    """`05 §12.3` — 10종 (`message.created` 포함). `ping` 은 §12.2 의 하트비트라 목록에 없다."""
    assert set(_sse_events_from_contract()) == set(sse_manager.SSE_EVENTS)
    assert len(sse_manager.SSE_EVENTS) == 10
    assert sse_manager.SSE_PING not in sse_manager.SSE_EVENTS
