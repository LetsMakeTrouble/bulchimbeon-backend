"""SQLAlchemy 모델 (`04 §2` 의 테이블과 1:1).

⚠️ **새 모델 파일을 만들면 반드시 여기에 import 를 추가한다.**
이 모듈이 `Base.metadata` 에 테이블을 등록하는 유일한 지점이고, 아래 두 곳이 이 모듈만 본다:

- `alembic/env.py` — 빠뜨리면 `alembic revision --autogenerate` 가 **에러 없이 빈 리비전**을 뱉는다.
- `tests/conftest.py` — `Base.metadata.create_all` 이 테이블을 안 만들어도 테스트는 초록이다.

둘 다 조용히 실패하므로 누락을 알아채기 어렵다. `tests/test_models.py` 가 이 등록 여부를 지킨다.
"""

from app.models.document import Chunk, Document, DocumentVersion
from app.models.event import Event
from app.models.notification import Notification
from app.models.official_qa import OfficialQA
from app.models.project import Guideline, Project, ProjectMember
from app.models.question import Answer, AnswerCitation, Question
from app.models.review_card import Feedback, ReviewCard
from app.models.user import User

__all__ = [
    "Answer",
    "AnswerCitation",
    "Chunk",
    "Document",
    "DocumentVersion",
    "Event",
    "Feedback",
    "Guideline",
    "Notification",
    "OfficialQA",
    "Project",
    "ProjectMember",
    "Question",
    "ReviewCard",
    "User",
]
