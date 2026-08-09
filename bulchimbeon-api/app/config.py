"""환경설정(env)과 프로젝트 기본 설정(DEFAULT_SETTINGS)의 단일 원천.

- env 는 `03 §4`의 `.env.example`과 1:1이다.
- `DEFAULT_SETTINGS` 는 `04 §3`의 `projects.settings` 기본값이며,
  **임계값의 유일한 정의 위치**다 (`CLAUDE.md` 룰 3). 다른 파일에 상수로 복사하지 않는다 —
  런타임에는 `projects.settings`에서 로드한다.
"""

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 벡터 컬럼은 vector(1536) 리터럴 고정이다 (`04` 문서 상단).
# EMBEDDING_DIM env 는 (a) 임베딩 응답 차원 검증 (b) OpenAI `dimensions` 파라미터 전달용이며
# 컬럼 차원을 바꾸는 스위치가 **아니다**. 다르면 기동 시점에 죽는다.
EMBEDDING_DIM_FIXED = 1536

# --------------------------------------------------------------------------------------
# 업로드 제한 (`03 §7`) — 파일당 20MB, 프로젝트당 문서 100개.
#
# ⚠️ `03 §7` 은 이 둘을 "(설정화)" 로 적었지만 **`projects.settings` 키가 될 수 없다.**
#    허용 키는 `04 §3` = `05 §3` 의 16개로 닫혀 있고(계약서 표 밖의 키는 400), 프론트와의
#    계약을 임의로 늘리는 것은 금지다. 우선순위 규칙(`05` > `03`)대로 계약서를 따르고
#    여기 상수로 둔다. 룰 3 의 임계값 목록(등급·유사도·만료 등)에는 해당하지 않는다.
# --------------------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DOCUMENTS_PER_PROJECT = 100

# 폼 파싱 **이전**에 거는 바깥 관문 (`app/core/upload_limit.py`).
# 본문 전체(멀티파트 경계·title·auto_activate 필드 포함) 기준이므로 파일 하나의 상한보다
# 약간 넉넉해야 정확히 20MB 인 파일이 경계 오버헤드 때문에 거절되지 않는다.
# 파일 하나의 정확한 판정은 여전히 document_service._read_capped 가 한다.
MULTIPART_OVERHEAD_ALLOWANCE = 1024 * 1024
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_ALLOWANCE

# --------------------------------------------------------------------------------------
# projects.settings 기본값 (04 §3) — 임계값 5종은 M-1 게이트 실측 확정값(2026-08-07)이다.
# 판정 표 원문: calibration-2026-08-07.txt / -latency.txt / -sql.txt
#
# ⚠️ `briefing_timezone` 키는 존재하지 않는다. 브리핑·DND 시각 판정의 단일 원천은
#    담당자(`projects.answerer_id`)의 `users.timezone` 이다 (04 §3).
# --------------------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    "green_threshold": 80,  # 🟢 하한 (등급 스케일 0~100)
    "yellow_threshold": 50,  # 🟡 하한
    "grounding_min": 60,  # 문장 3개 이상일 때만 적용
    "s_floor": 0.25,  # 유사도 리스케일 하한 — M-1 손 조정값
    "s_ceil": 0.679,  # 유사도 리스케일 상한 — M-1 관측 상위 10% 지점
    "similarity_floor": 0.423,  # 미만이면 강제 🔴 no_evidence (원시 코사인 기준)
    "reuse_threshold": 0.925,  # 재사용 판정 — 리스케일하지 않은 원시 코사인
    "similar_threshold": 0.855,  # 유사 질문 판정 — 원시 코사인
    "draft_expire_hours": 72,  # draft 만료 스위퍼 (D14)
    "max_lessons": 30,  # 프로젝트당 교훈 상한
    "retrieval_top_k": 6,  # 벡터 검색 top-k (8 아님)
    "daily_llm_call_limit": 500,  # 초과 시 강제 🔴 quota_exceeded. env 가 아니라 여기다
    "saved_wait_assumption_hours": 24,  # 절약 대기시간 지표의 가정치
    "briefing_hour": 9,  # 담당자 timezone 기준 브리핑 시각
    "dnd_start": "22:00",
    "dnd_end": "07:00",
}


class Settings(BaseSettings):
    """`.env` 기반 환경설정 (`03 §4`)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env 에는 M-1이 남긴 DEPLOY_DATABASE_URL 처럼 앱이 쓰지 않는 키도 있다.
        extra="ignore",
    )

    # App
    app_env: str = "local"  # local | demo
    api_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"  # 쉼표 구분 — 목록은 cors_origin_list
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # DB
    database_url: str = "postgresql+asyncpg://bulchimbeon:bulchimbeon@localhost:5432/bulchimbeon"
    test_database_url: str = (
        "postgresql+asyncpg://bulchimbeon:bulchimbeon@localhost:5432/bulchimbeon_test"
    )

    # LLM — 모델명은 전부 env. 프로바이더 추상화로 교체 가능 (03 §1)
    llm_provider: str = "openai"  # openai | fake
    openai_api_key: str = ""
    # --- 단계별 모델 배정 (사용자 결정 2026-08-09) -------------------------------------
    # 기준은 "중요도"가 아니라 **실패했을 때 회복 가능한가**다.
    # 뒤에 걸러 줄 단계가 있으면 싼 모델로 충분하고, 없으면 비싼 모델을 쓴다.
    llm_model_answer: str = "gpt-5.6-terra"  # ④ 답변 생성 — 뒤에 ⑤가 걸러 준다
    llm_model_verify: str = "gpt-5.6-sol"  # ⑤ 근거 검증 — 뚫리면 환각이 🟢로 발행된다
    llm_model_reuse_gate: str = "gpt-5.6-sol"  # 재사용 판정 — 뚫리면 틀린 확정답이 퍼진다
    llm_model_struct: str = "gpt-5.6-terra"  # ⑦ 카드 구조화 — 담당자가 읽고 보정한다
    llm_model_translate: str = "gpt-5.6-luna"  # ① 질문 ko→en — 틀려도 등급만 떨어진다
    llm_model_lesson: str = "gpt-5.6-luna"  # 교훈 추출 — 배치, 지연·정확도 여유 있음
    # ⚠️ **담당자 확정문 en→ko 는 번역과 같은 슬롯에 두지 않는다.**
    # 이 번역 결과가 곧 질문자가 읽는 확정 답변이고, 룰 4(확정 ko 원문 재번역 금지)에 따라
    # 공식 Q&A 로 그대로 굳어 재사용된다. 담당자는 영어로 쓰고 질문자는 한국어를 읽으므로
    # **오역을 잡아 줄 사람이 경로에 없다.** 싼 모델로 내리면 안 되는 이유다.
    llm_model_answer_translate: str = "gpt-5.6-terra"

    # ⚠️ **`minimal` 은 gpt-5.6 계열에서 400 이다** (2026-08-09 실측).
    # `minimal` 은 gpt-5-mini 전용이었다. 5.6 계열의 최소값은 `low` 이며, 실측상 `low` 로도
    # 데드라인(🟢🟡 25s · 🔴 35s)에 여유가 크다 — terra p50 1.2s / sol p50 1.7s (근거 6청크).
    # gpt-5-mini 도 `low` 를 받으므로 두 계열을 섞어 써도 이 값 하나로 동작한다.
    llm_reasoning_effort: str = "low"
    llm_timeout_seconds: int = 45
    llm_pipeline_deadline_seconds: int = 25  # 🟢/🟡 경로
    llm_pipeline_deadline_red_seconds: int = 35  # 🔴 경로 (⑦구조화 1회 추가)
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = EMBEDDING_DIM_FIXED

    # Storage
    storage_dir: Path = PROJECT_ROOT / "storage"

    # Integrations
    integration_encryption_key: str = "change-me-32bytes"

    @field_validator("embedding_dim")
    @classmethod
    def _embedding_dim_must_match_column(cls, value: int) -> int:
        """기동 시 fail-fast (`04` 문서 상단).

        벡터 컬럼은 `vector(1536)` 리터럴 고정이므로 env 로 차원을 바꿀 수 없다.
        조용히 다른 값을 허용하면 임베딩 삽입이 런타임에 터진다.
        """
        if value != EMBEDDING_DIM_FIXED:
            raise ValueError(
                f"EMBEDDING_DIM must be {EMBEDDING_DIM_FIXED} (got {value}). "
                "벡터 컬럼은 vector(1536) 리터럴 고정이며 이 env 로 차원을 바꿀 수 없다."
            )
        return value

    @field_validator("storage_dir")
    @classmethod
    def _storage_dir_must_be_absolute(cls, value: Path) -> Path:
        """상대 경로는 uvicorn 실행 위치·컨테이너 WORKDIR에 따라 다른 곳을 가리킨다 (`03 §5.2`)."""
        if not value.is_absolute():
            raise ValueError(f"STORAGE_DIR must be an absolute path (got {value!r}).")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
