"""users (`04 §2`)."""

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# `05 §1.3` 은 role 만 enum 으로 못박지만, 언어는 `02 §8` 이 한↔영 고정이다.
LANGUAGES = ("ko", "en")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("language IN ('ko', 'en')", name="ck_users_language"),)

    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="ko")

    # ⚠️ 브리핑·DND 시각 판정의 **단일 원천**이다 (`02 §6`, `04 §3`).
    # `projects.settings` 에 타임존 키를 두지 않으므로,
    # 담당자가 교체되면 판정 기준도 여기를 따라 옮겨간다.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Seoul")
