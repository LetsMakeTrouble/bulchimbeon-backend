"""대화 메시지 (`05 §6.1`) — 사람 간 양방향 채널.

질문 파이프라인과 완전히 분리돼 있다. AI 도 알림·브리핑도 붙지 않는다 — 상태 변화 기록
(events, 룰 4)과 SSE 갱신 신호(`message.created`)만 낸다. 커밋은 라우터가 한다.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation_message import ConversationMessage
from app.models.project import MEMBER_STATUS_ACTIVE, ProjectMember
from app.models.user import User
from app.schemas.message import MessageCreate, MessageListResponse, MessageOut, MessageSender
from app.services import event_service, sse_manager

_MAX_LIMIT = 100


async def create_message(
    db: AsyncSession,
    *,
    project_id: UUID,
    member: ProjectMember,
    sender: User,
    payload: MessageCreate,
) -> MessageOut:
    """멤버면 역할 무관 발화할 수 있다 — 담당자 발화 채널이 이 API 의 존재 이유다.

    SSE 는 **프로젝트의 활성 멤버 전원**에게 간다. 발신자도 포함이다 — `card.resolved` 가
    같은 이유(다른 기기·다른 탭 동기화)로 행위자 본인에게 발행된다 (`05 §12.3`).
    """
    message = ConversationMessage(
        project_id=project_id, sender_id=sender.id, content=payload.content
    )
    db.add(message)
    await db.flush()

    # 룰 4 — 상태 변화는 events 에 남긴다. member.joined 처럼 자체 entity_type 을 쓴다.
    await event_service.record_event(
        db,
        project_id=project_id,
        type=event_service.EVENT_MESSAGE_CREATED,
        actor_id=sender.id,
        entity_type="message",
        entity_id=message.id,
    )

    recipient_ids = await _active_member_ids(db, project_id)
    sse_manager.queue_message_created(
        db, user_ids=recipient_ids, message_id=message.id, project_id=project_id
    )

    return MessageOut(
        id=message.id,
        content=message.content,
        sender=MessageSender(id=sender.id, name=sender.name, role=member.role),
        created_at=message.created_at,
    )


async def list_messages(
    db: AsyncSession, *, project_id: UUID, limit: int = 20, offset: int = 0
) -> MessageListResponse:
    """목록 (`05 §1.2` 봉투) — **created_at 오름차순**, 채팅 화면이 위에서 아래로 읽힌다.

    발신자 이름·역할은 조인 한 번으로 싣는다 (N+1 금지). 탈퇴 멤버(D18)의 행도
    `project_members` 에 남으므로(행 삭제가 아니라 status 전환) 조인이 비지 않는다.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    total = (
        await db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.project_id == project_id)
        )
        or 0
    )

    rows = (
        await db.execute(
            select(ConversationMessage, User.name, ProjectMember.role)
            .join(User, User.id == ConversationMessage.sender_id)
            .join(
                ProjectMember,
                (ProjectMember.project_id == ConversationMessage.project_id)
                & (ProjectMember.user_id == ConversationMessage.sender_id),
            )
            .where(ConversationMessage.project_id == project_id)
            # 같은 시각(이론상)엔 id 로 순서를 고정한다 — 목록 정렬 규약과 동일.
            .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return MessageListResponse(
        items=[
            MessageOut(
                id=message.id,
                content=message.content,
                sender=MessageSender(id=message.sender_id, name=name, role=role),
                created_at=message.created_at,
            )
            for message, name, role in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _active_member_ids(db: AsyncSession, project_id: UUID) -> list[UUID]:
    """SSE 수신자 — 활성 멤버 전원. 탈퇴 멤버(D18)는 비멤버와 같다 (`core/deps.py`)."""
    rows = await db.scalars(
        select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
    )
    return list(rows.all())
