"""프로필 정합성 — DB·LLM 없이 도는 순수 검사.

시드는 25~30분과 실 LLM 비용을 쓴다. **그 전에 깨질 수 있는 것들**을 여기서 깨뜨린다:
질문이 없는 문서를 가리키거나, 이력이 정의되지 않은 계열을 던지거나, 이력이 일일 호출
상한을 넘기는 경우다. 셋 다 시드를 끝까지 돌린 뒤에야 드러나면 그 시간을 잃는다.
"""

from collections import Counter

import pytest

from app.config import DEFAULT_SETTINGS
from scripts import demo_profiles
from scripts.demo_profiles import DemoProfile

# 질문당 평균 LLM 호출 (`seed.py` 독스트링 실측 2.94 를 보수적으로 올려 잡는다).
CALLS_PER_QUESTION = 3

PROFILES = list(demo_profiles.PROFILES.values())
IDS = [profile.key for profile in PROFILES]


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_corpus_files_exist(profile: DemoProfile) -> None:
    """코퍼스 파일이 실제로 있다. 없으면 시드가 업로드 도중 죽는다."""
    for filename, _ in profile.documents:
        path = profile.seed_dir / filename
        assert path.exists(), f"{profile.key}: 시드 문서가 없다 — {path}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_corpus_has_no_crlf(profile: DemoProfile) -> None:
    """CRLF 가 섞이면 `content_hash` 가 OS 마다 달라진다 (`03 §5.2`)."""
    for filename, _ in profile.documents:
        payload = (profile.seed_dir / filename).read_bytes()
        assert b"\r\n" not in payload, f"{profile.key}: CRLF — {filename}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_evidence_docs_are_in_corpus(profile: DemoProfile) -> None:
    """기대 근거 문서가 코퍼스 안에 있다.

    파일명을 고치고 질문셋을 안 고치면 여기서 걸린다 — 안 걸리면 그 질문은 조용히
    "기대 근거를 영원히 못 찾는" 상태가 된다.
    """
    filenames = {filename for filename, _ in profile.documents}
    for question in profile.canonical:
        if question.evidence_doc is None:  # 강제 🔴 — 근거가 없는 것이 정답이다.
            continue
        assert question.evidence_doc in filenames, f"{profile.key}/{question.key}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_history_families_are_defined(profile: DemoProfile) -> None:
    """이력의 모든 계열이 질문셋에 있다. 없으면 `report_grades` 가 KeyError 로 죽는다."""
    for item in profile.history:
        assert item.family in profile.by_key, f"{profile.key}: 모르는 계열 {item.family}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_history_fits_daily_call_limit(profile: DemoProfile) -> None:
    """이력이 일일 LLM 호출 상한 안에 들어간다.

    넘기면 뒷부분이 **조용히** 강제 🔴 `quota_exceeded` 가 된다 — 등급 분포가 무너진
    이유가 질문 문안이 아니라 상한이었다는 것을 알아채기 어렵다.
    """
    limit = int(
        profile.settings_overrides.get(
            "daily_llm_call_limit", DEFAULT_SETTINGS["daily_llm_call_limit"]
        )
    )
    estimate = len(profile.history) * CALLS_PER_QUESTION
    assert estimate <= limit, (
        f"{profile.key}: 이력 {len(profile.history)}건 ≈ {estimate}회 > 상한 {limit}회 — "
        "질문을 줄이거나 `settings_overrides` 로 상한을 올려라."
    )


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_live_only_originals_are_not_in_history(profile: DemoProfile) -> None:
    """라이브 시연이 쓰는 원문은 이력이 미리 소진하지 않는다 (`09 §2`).

    ⚠️ 판정 기준은 **`live_only_keys`** 다. `no_approval_keys`·`no_red_resolve_keys` 는 다른
    축이라 그걸로 재면 멀쩡한 데이터가 위반으로 잡힌다 — GlobalMart 의 Q2·Q7 은 승인만
    막고 원문은 이력에 넣는 것이 사양이다.

    ⚠️ 판정 대상도 `content_ko` 다. 계열이 이력에 등장하는 것 자체는 정상이고(변형은 무방),
    **원문 문장**이 들어간 것만 문제다.
    """
    live_texts = {
        profile.by_key[key].content_ko
        for key in profile.live_only_keys
        if key in profile.by_key
    }
    planted = {item.content_ko for item in profile.history} & live_texts
    assert not planted, f"{profile.key}: 라이브 원문이 이력에 들어갔다 — {planted}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_green_sample_is_reachable(profile: DemoProfile) -> None:
    """🟢 기대 계열의 이력 건수가 표본 하한(D25)보다 넉넉하다.

    `accuracy_service.MIN_SAMPLE` 에 못 미치면 지표 화면이 전 등급 "표본 부족"으로 떠서
    발표 마지막 화면이 빈다. 실제 발행률이 100% 는 아니므로 **두 배**를 요구한다.
    """
    from app.services import accuracy_service

    counts = Counter(item.family for item in profile.history)
    green_families = [
        question.key for question in profile.canonical if "green" in question.expected_grades
    ]
    total = sum(counts[key] for key in green_families)
    assert total >= accuracy_service.MIN_SAMPLE * 2, (
        f"{profile.key}: 🟢 기대 계열 이력이 {total}건뿐이다 "
        f"(권장 {accuracy_service.MIN_SAMPLE * 2}건 이상)"
    )


def test_payload_uuid_round_trip() -> None:
    """`events.payload` 안의 UUID 가 내보내기·불러오기를 지나 그대로 이어진다.

    ⚠️ 이게 깨지면 **화면은 멀쩡하고 숫자만 틀린다.** 지표가 `payload['card_id']` 로 카드를
    되짚기 때문에(`metrics_service`), 매핑이 어긋나면 카드 30초 처리율이 0 으로 나온다.
    """
    from uuid import uuid4

    from scripts import history_fixture

    card_id, answer_id, stranger = uuid4(), uuid4(), uuid4()
    refs = {card_id: "review_cards:2", answer_id: "answers:7"}
    payload = {
        "card_id": str(card_id),
        "nested": [{"answer_id": str(answer_id)}, "그냥 문자열"],
        # 내보내지 않은 UUID 는 손대지 않는다 — 남의 프로젝트를 가리키게 만들면 안 된다.
        "unrelated": str(stranger),
        "grade": "green",
    }

    dumped = history_fixture._replace_uuids(payload, refs)
    assert dumped["card_id"] == "__ref:review_cards:2"
    assert dumped["unrelated"] == str(stranger)

    new_ids = {"review_cards:2": uuid4(), "answers:7": uuid4()}
    restored = history_fixture._restore_uuids(dumped, new_ids)
    assert restored["card_id"] == str(new_ids["review_cards:2"])
    assert restored["nested"][0]["answer_id"] == str(new_ids["answers:7"])
    assert restored["nested"][1] == "그냥 문자열"
    assert restored["unrelated"] == str(stranger)
    assert restored["grade"] == "green"


def test_fixture_excludes_usage_and_vectors() -> None:
    """픽스처가 옮기지 않기로 한 것들이 실제로 목록 밖에 있다.

    `llm_usage` 를 심으면 오늘자 사용량으로 잡혀 라이브 질문이 상한에 걸린다.
    """
    from scripts import history_fixture

    names = {model.__tablename__ for model in history_fixture.TABLES}
    assert "llm_usage" not in names
    assert "briefing_runs" not in names
    assert "chunks" not in names  # 청크는 인제스트가 만든다 — 픽스처가 심지 않는다.


def test_default_profile_is_the_calibrated_one() -> None:
    """기본값은 임계값을 실측한 코퍼스다 (`08 §1`).

    기본값이 옮겨 가면 `--profile` 을 안 준 모든 실행·문서·대본이 조용히 다른 데모를
    가리킨다. 바꾸려면 이 테스트를 먼저 바꿔야 한다.
    """
    assert demo_profiles.DEFAULT_PROFILE is demo_profiles.GLOBALMART
    assert not demo_profiles.GLOBALMART.settings_overrides
