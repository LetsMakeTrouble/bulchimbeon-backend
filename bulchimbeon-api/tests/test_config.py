"""config.py 계약 — DEFAULT_SETTINGS 와 fail-fast (`04 §3`, `03 §4`).

⚠️ 기대값을 여기에 다시 적지 않는다. 룰 3은 기본값이 **한 곳**에만 있으라는 규칙인데,
테스트가 임계값을 복사해 두면 그게 곧 두 번째 원천이 된다. 대신 `docs/04-data-model.md §3`
을 파싱해 비교한다 — 문서가 사양이므로 코드와 문서가 갈라지는 순간 이 테스트가 깨진다.
"""

import json
import re

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_SETTINGS, EMBEDDING_DIM_FIXED, PROJECT_ROOT, Settings

DATA_MODEL_DOC = PROJECT_ROOT / "docs" / "04-data-model.md"


def _settings_schema_from_doc() -> dict:
    """`04 §3`의 projects.settings 기본값 JSON 블록을 읽는다."""
    doc = DATA_MODEL_DOC.read_text(encoding="utf-8")
    match = re.search(r"## 3\. projects\.settings.*?```json\n(.*?)```", doc, re.S)
    assert match is not None, f"{DATA_MODEL_DOC} 에서 §3 기본값 블록을 찾지 못했다"
    return json.loads(match.group(1))


def test_default_settings_matches_data_model_doc() -> None:
    """키 집합과 값이 문서와 정확히 일치해야 한다."""
    spec = _settings_schema_from_doc()

    assert set(spec) == set(DEFAULT_SETTINGS)
    assert spec == DEFAULT_SETTINGS


def test_default_settings_has_sixteen_keys() -> None:
    assert len(DEFAULT_SETTINGS) == 16


def test_briefing_timezone_is_not_a_setting() -> None:
    """브리핑·DND 시각의 단일 원천은 담당자의 `users.timezone` 이다 (`04 §3`)."""
    assert "briefing_timezone" not in DEFAULT_SETTINGS
    assert "briefing_timezone" not in _settings_schema_from_doc()


def test_embedding_dim_mismatch_fails_fast() -> None:
    """벡터 컬럼은 vector(1536) 리터럴 고정 — env 로 차원을 바꿀 수 없다 (`04` 문서 상단)."""
    with pytest.raises(ValidationError, match="EMBEDDING_DIM must be"):
        Settings(embedding_dim=768)

    assert Settings(embedding_dim=EMBEDDING_DIM_FIXED).embedding_dim == EMBEDDING_DIM_FIXED


def test_storage_dir_must_be_absolute() -> None:
    """상대 경로는 uvicorn 실행 위치·컨테이너 WORKDIR에 따라 다른 곳을 가리킨다 (`03 §5.2`)."""
    with pytest.raises(ValidationError, match="absolute path"):
        Settings(storage_dir="storage")


def test_cors_origins_parses_comma_separated_list() -> None:
    settings = Settings(cors_origins="http://a.example, http://b.example ,")
    assert settings.cors_origin_list == ["http://a.example", "http://b.example"]
