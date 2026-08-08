"""인증·권한 의존성 (`03 §2` 원칙 1, 룰 5 — 권한은 서버가 강제한다).

라우터는 이 의존성만 걸고 본문에서 역할을 다시 검사하지 않는다.
`require_*` 는 전부 **활성 멤버십**(`status='active'`)을 요구한다.
탈퇴한 멤버(D18)는 비멤버와 같다.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenRole, NotFound, NotMember, Unauthorized
from app.core.security import ACCESS_TOKEN_TYPE, access_token_expires_at, decode_token
from app.database import get_db
from app.models.document import Document
from app.models.integration import Integration
from app.models.lesson import LESSON_STATUS_DELETED, Lesson
from app.models.official_qa import OfficialQA
from app.models.project import (
    MEMBER_STATUS_ACTIVE,
    ROLE_ANSWERER,
    ROLE_ASKER,
    Project,
    ProjectMember,
)
from app.models.question import Answer, Question
from app.models.review_card import ReviewCard
from app.models.user import User

# auto_error=False — 헤더가 없을 때 starlette 이 403 을 던지지 않게 하고
# 우리가 401 UNAUTHORIZED 로 통일한다 (`05 §1.4`).
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise Unauthorized()

    user_id = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN_TYPE)
    user = await db.get(User, user_id)
    if user is None:
        # 서명은 유효하지만 유저가 사라진 토큰. 401 로 재로그인을 유도한다.
        raise Unauthorized()
    return user


async def get_access_token_expiry(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> datetime:
    """현재 access token 의 만료 시각 — `POST /sse/ticket` 만 쓴다 (`05 §12.2` 4번).

    스트림은 이 시각에 서버가 끊고 프론트는 refresh 후 새 티켓으로 재연결한다. `get_current_user`
    와 헤더를 두 번 읽지만, 만료 시각을 유저 객체에 얹어 다니면 모든 라우터가 쓰지 않는 값을
    들고 다니게 된다.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthorized()
    return access_token_expires_at(credentials.credentials)


async def _active_membership(db: AsyncSession, project_id: UUID, user: User) -> ProjectMember:
    """활성 멤버십을 돌려준다. 없으면 404(프로젝트 없음) 또는 403 `NOT_MEMBER`."""
    member = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
    )
    if member is not None:
        return member

    # 멤버십이 없을 때만 프로젝트 존재를 확인한다 — 존재하면 403, 아니면 404 다.
    project_exists = await db.scalar(select(Project.id).where(Project.id == project_id))
    if project_exists is None:
        raise NotFound()
    raise NotMember()


async def require_member(
    project_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectMember:
    return await _active_membership(db, project_id, user)


async def require_answerer(member: ProjectMember = Depends(require_member)) -> ProjectMember:
    if member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return member


async def require_asker(member: ProjectMember = Depends(require_member)) -> ProjectMember:
    """질문자 전용 액션.

    담당자는 자기 프로젝트에 질문할 수 없고(D17), 교체 없이 떠날 수도 없다(D18 반대편) —
    M3 의 질문 접수와 M1 의 `POST /projects/{id}/leave` 가 같은 의존성을 공유한다.
    """
    if member.role != ROLE_ASKER:
        raise ForbiddenRole("질문자만 수행할 수 있습니다.")
    return member


@dataclass(frozen=True)
class DocumentAccess:
    """문서 경로(`/documents/{id}/…`)의 권한 확인 결과 (`05 §4`).

    경로에 `project_id` 가 없으므로 문서를 먼저 읽어 소속 프로젝트를 알아낸 뒤 멤버십을 본다.
    라우터가 문서를 다시 조회하지 않도록 함께 돌려준다.
    """

    document: Document
    member: ProjectMember


async def require_document_member(
    document_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentAccess:
    """멤버 읽기 (`05 §4` — 담당자 전용 쓰기, 멤버 읽기).

    ⚠️ 존재하지 않는 문서와 **남의 프로젝트 문서**를 똑같이 404 로 돌려준다.
    403 을 주면 "그 id 의 문서가 존재한다"는 사실이 비멤버에게 새어 나간다.
    """
    document = await db.get(Document, document_id)
    if document is None:
        raise NotFound()

    try:
        member = await _active_membership(db, document.project_id, user)
    except NotMember:
        raise NotFound() from None
    return DocumentAccess(document=document, member=member)


async def require_document_answerer(
    access: DocumentAccess = Depends(require_document_member),
) -> DocumentAccess:
    """담당자 전용 쓰기 (`05 §4`)."""
    if access.member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return access


@dataclass(frozen=True)
class IntegrationAccess:
    """`/integrations/{id}` 경로의 권한 확인 결과 (`05 §5` — 담당자 전용)."""

    integration: Integration
    member: ProjectMember


async def require_integration_answerer(
    integration_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IntegrationAccess:
    """`05 §5` 는 연동 전 경로가 **담당자 전용**이다 — 동기화·해제 모두 담당자만 한다.

    ⚠️ 남의 프로젝트 연동은 **404** 다(문서·질문 경로와 같은 규약). 연동에는 토큰이 붙어
    있으므로 존재 여부조차 흘리지 않는다. 같은 프로젝트의 질문자는 403 이다.
    """
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise NotFound()

    try:
        member = await _active_membership(db, integration.project_id, user)
    except NotMember:
        raise NotFound() from None

    if member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return IntegrationAccess(integration=integration, member=member)


@dataclass(frozen=True)
class QuestionAccess:
    """`/questions/{id}` 경로의 권한 확인 결과 (`05 §6`).

    문서 경로와 같은 이유로 질문을 먼저 읽어 소속 프로젝트를 알아낸다.
    라우터가 질문을 다시 조회하지 않도록 함께 돌려준다.
    """

    question: Question
    member: ProjectMember


async def require_question_member(
    question_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuestionAccess:
    """멤버 읽기 (`05 §6`).

    ⚠️ 남의 프로젝트 질문도 **404** 다 — 403 을 주면 "그 id 의 질문이 존재한다"는 사실이
    비멤버에게 새어 나간다 (문서 경로와 같은 규약).
    """
    question = await db.get(Question, question_id)
    if question is None:
        raise NotFound()

    try:
        member = await _active_membership(db, question.project_id, user)
    except NotMember:
        raise NotFound() from None
    return QuestionAccess(question=question, member=member)


async def require_question_author(
    access: QuestionAccess = Depends(require_question_member),
    user: User = Depends(get_current_user),
) -> QuestionAccess:
    """질문 **작성자** 전용 (`05 §6` PATCH /questions/{id}).

    담당자라도 남의 질문의 긴급도를 바꿀 수 없다 — 긴급 여부를 정하는 주체는 질문자다 (D10).
    """
    if access.question.asker_id != user.id:
        raise ForbiddenRole("질문 작성자만 수행할 수 있습니다.")
    return access


@dataclass(frozen=True)
class AnswerAccess:
    """`/answers/{id}/feedback` 의 권한 확인 결과 (`05 §6`)."""

    answer: Answer
    question: Question
    member: ProjectMember


async def require_answer_asker(
    answer_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnswerAccess:
    """크로스체크는 **질문자만** 한다 (`05 §6`).

    확정은 담당자의 권한이고 질문자가 할 수 있는 건 "맞았다 / 달랐다"뿐이다 (룰 3).
    같은 프로젝트의 다른 질문자도 피드백할 수 있다 — "맞았다 2건"이 성립하려면 그래야 한다
    (`04 §7` UNIQUE(answer_id, user_id)).
    """
    answer = await db.get(Answer, answer_id)
    if answer is None:
        raise NotFound()

    question = await db.get(Question, answer.question_id)
    if question is None:
        raise NotFound()

    try:
        member = await _active_membership(db, question.project_id, user)
    except NotMember:
        raise NotFound() from None

    if member.role != ROLE_ASKER:
        raise ForbiddenRole("질문자만 수행할 수 있습니다.")
    return AnswerAccess(answer=answer, question=question, member=member)


@dataclass(frozen=True)
class CardAccess:
    """`/review-cards/{id}` 의 권한 확인 결과 (`05 §7` — 담당자 전용)."""

    card: ReviewCard
    member: ProjectMember


async def require_card_answerer(
    card_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardAccess:
    """확인 카드는 **담당자 전용**이다 (`05 §7`).

    ⚠️ 남의 프로젝트 카드는 404 다(문서·질문 경로와 같은 규약). 같은 프로젝트의 질문자는
    카드의 존재를 알아도 되는 위치이므로 403 이다 — 큐가 담당자 화면이라는 사실 자체는
    계약서에 공개돼 있다.
    """
    card = await db.get(ReviewCard, card_id)
    if card is None:
        raise NotFound()

    try:
        member = await _active_membership(db, card.project_id, user)
    except NotMember:
        raise NotFound() from None

    if member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return CardAccess(card=card, member=member)


@dataclass(frozen=True)
class OfficialQAAccess:
    """`/official-qas/{id}` 의 권한 확인 결과 (`05 §9`)."""

    official_qa: OfficialQA
    member: ProjectMember


async def require_official_qa_member(
    official_qa_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OfficialQAAccess:
    """확정 지식 열람은 멤버 전체에게 열려 있다 (`05 §9`)."""
    official_qa = await db.get(OfficialQA, official_qa_id)
    if official_qa is None:
        raise NotFound()

    try:
        member = await _active_membership(db, official_qa.project_id, user)
    except NotMember:
        raise NotFound() from None
    return OfficialQAAccess(official_qa=official_qa, member=member)


async def require_official_qa_answerer(
    access: OfficialQAAccess = Depends(require_official_qa_member),
) -> OfficialQAAccess:
    """`DELETE /official-qas/{id}` 는 담당자 전용이다 (`05 §9`)."""
    if access.member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return access


@dataclass(frozen=True)
class LessonAccess:
    """`/lessons/{id}` 의 권한 확인 결과 (`05 §10`)."""

    lesson: Lesson
    member: ProjectMember


async def require_lesson_member(
    lesson_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LessonAccess:
    """교훈을 읽어 소속 프로젝트를 알아낸 뒤 멤버십을 본다 (공식 Q&A 경로와 같은 규약).

    ⚠️ 남의 프로젝트 교훈은 **404** 다 — 403 을 주면 그 id 의 존재가 새어 나간다.

    ⚠️ **삭제된 교훈도 404** 다. `status='deleted'` 행은 `content_hash` 대조로 재생성을
    막기 위한 묘비일 뿐이고(D8), `05 §10` 의 `status` 어휘는 candidate·approved 뿐이다 —
    되살릴 수 있으면 담당자가 버린 원칙이 승인으로 부활해 D8 이 무력화된다.
    """
    lesson = await db.get(Lesson, lesson_id)
    if lesson is None or lesson.status == LESSON_STATUS_DELETED:
        raise NotFound()

    try:
        member = await _active_membership(db, lesson.project_id, user)
    except NotMember:
        raise NotFound() from None
    return LessonAccess(lesson=lesson, member=member)


async def require_lesson_answerer(
    access: LessonAccess = Depends(require_lesson_member),
) -> LessonAccess:
    """`05 §10` 은 교훈 전 경로가 **담당자 전용**이다 — 승인·삭제 모두 담당자만 한다 (룰 7)."""
    if access.member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return access
