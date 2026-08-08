"""SSE 발행 지점 (`05 §12.3`).

⚠️ **M2 시점에는 구독자 큐가 아직 없다** — M5(알림·SSE)가 인메모리 큐와
`GET /sse/stream` 을 만든다. 그럼에도 발행 **호출부는 지금 만든다**:
"폴링 규약만 남기고 이벤트를 생략" 하면 프론트가 문서 목록을 갱신할 신호를 잃는다
(`05 §4` — 인제스트 완료는 SSE 로 통지된다).

M5 는 이 함수의 **몸통만** 갈아끼우면 된다. 호출부는 건드리지 않는다.

⚠️ `--workers 1` 고정 전제 (룰 9). 구독자 큐가 인메모리라 워커가 둘이면
다른 워커에 붙은 클라이언트에게 이벤트가 가지 않는다.
"""

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# `05 §12.3` 이벤트 목록. 계약서에 없는 이벤트명을 새로 만들지 않는다.
SSE_DOCUMENT_INGESTED = "document.ingested"
SSE_ANSWER_COMPLETED = "answer.completed"


async def publish(*, project_id: UUID, event: str, data: dict[str, Any]) -> None:
    """프로젝트 구독자에게 SSE 이벤트를 발행한다.

    TODO(M5): 인메모리 구독자 큐로 배선한다. 지금은 로그만 남긴다 —
    호출부(`services/pipeline/ingest.py`)는 그대로 두고 여기만 채우면 된다.
    """
    logger.info("sse publish (M5 배선 대기): project=%s event=%s data=%s", project_id, event, data)


async def publish_document_ingested(
    *, project_id: UUID, document_id: UUID, version_id: UUID, status: str
) -> None:
    """`05 §12.3` — `document.ingested` `{document_id, version_id, status}` (수신자: 담당자).

    `status` 는 `ready` | `failed` 다. **양쪽 모두 발행한다** — 실패를 알리지 않으면
    프론트가 `pending` 상태로 영원히 폴링한다.
    """
    await publish(
        project_id=project_id,
        event=SSE_DOCUMENT_INGESTED,
        data={
            "document_id": str(document_id),
            "version_id": str(version_id),
            "status": status,
        },
    )


async def publish_answer_completed(
    *, project_id: UUID, question_id: UUID, grade: str | None, status: str
) -> None:
    """`05 §12.3` — `answer.completed` `{question_id, grade, status}` (수신자: 질문자).

    🔴 보류(`status='held'`)와 파이프라인 실패(`status='failed'`)에도 **발행한다** —
    알리지 않으면 프론트가 `processing` 상태로 영원히 폴링한다. 🔴 은 `grade='red'` 이고
    실패는 `grade=None` 이다.

    프론트는 이 이벤트를 **갱신 신호로만** 쓰고 `GET /questions/{id}` 를 재조회한다 (`05 §12.2`).
    """
    await publish(
        project_id=project_id,
        event=SSE_ANSWER_COMPLETED,
        data={
            "question_id": str(question_id),
            "grade": grade,
            "status": status,
        },
    )
