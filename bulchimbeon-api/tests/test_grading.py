"""등급 산출 단위 테스트 (`02` 룰 1, `06 §2` ③⑤⑥).

파이프라인 e2e(`test_pipeline.py`)가 분기를 보는 곳이라면, 여기는 **공식 자체**를 본다.
임계값은 전부 인자로 넘긴다 — 이 파일에도, `grading.py` 에도 숫자가 박히면 룰 3 위반이다.
"""

import pytest

from app.config import DEFAULT_SETTINGS
from app.services.pipeline import dnd, grading

S_FLOOR = DEFAULT_SETTINGS["s_floor"]
S_CEIL = DEFAULT_SETTINGS["s_ceil"]


@pytest.mark.parametrize(
    ("sim_raw", "expected"),
    [
        (S_FLOOR, 0),  # 하한
        (S_CEIL, 100),  # 상한
        (0.0, 0),  # clamp 아래
        (1.0, 100),  # clamp 위
        (0.50, round((0.50 - S_FLOOR) / (S_CEIL - S_FLOOR) * 100)),
    ],
)
def test_search_score_rescales_between_floor_and_ceiling(sim_raw: float, expected: int) -> None:
    assert grading.search_score(sim_raw, s_floor=S_FLOOR, s_ceil=S_CEIL) == expected


def test_search_score_is_monotonic() -> None:
    """리스케일은 원시 유사도에 **단조**다.

    그래서 임계값을 움직여 강제 🔴 을 만들 수 없다 — Q3 를 올리면 Q8·Q9 도 함께 올라간다
    (`04 §3` 경고). 강제 🔴 은 `similarity_floor` 와 ④ 의 플래그가 담당한다.
    """
    scores = [
        grading.search_score(value / 100, s_floor=S_FLOOR, s_ceil=S_CEIL) for value in range(101)
    ]
    assert scores == sorted(scores)


def test_grounding_score_denominator_is_the_original_sentence_count() -> None:
    """5문장 중 2문장 supported → 40. **프루닝 후 개수(2)를 분모로 쓰면 100 이 된다.**"""
    assert grading.grounding_score(2, 5) == 40
    assert grading.grounding_score(2, 2) == 100, "분모를 바꾸면 이렇게 된다 — 쓰지 말 것"


def test_grounding_score_of_empty_answer_is_zero() -> None:
    assert grading.grounding_score(0, 0) == 0


def test_matching_rate_is_min_not_average() -> None:
    assert grading.matching_rate(100, 60) == 60
    assert grading.matching_rate(60, 100) == 60
    # 평균이었다면 80 → 🟢 이 됐을 값이다.
    assert grading.matching_rate(100, 60) != (100 + 60) // 2


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(100, "green"), (80, "green"), (79, "yellow"), (50, "yellow"), (49, "red"), (0, "red")],
)
def test_grade_thresholds(rate: int, expected: str) -> None:
    assert (
        grading.grade_for(
            rate,
            green_threshold=DEFAULT_SETTINGS["green_threshold"],
            yellow_threshold=DEFAULT_SETTINGS["yellow_threshold"],
        )
        == expected
    )


@pytest.mark.parametrize(
    ("count", "expected"), [(0, False), (1, True), (2, True), (3, False), (5, False)]
)
def test_all_supported_rule_applies_below_three_sentences(count: int, expected: bool) -> None:
    """1~2문장 구간은 비율 통계가 의미를 갖지 못한다 (룰 1)."""
    assert grading.requires_all_supported(count) is expected


def test_pruning_only_applies_to_three_or_more_sentences() -> None:
    grounding_min = DEFAULT_SETTINGS["grounding_min"]

    assert grading.should_prune(40, 5, grounding_min=grounding_min) is True
    assert grading.should_prune(60, 5, grounding_min=grounding_min) is False
    # 1~2문장은 별도 규칙이 처리한다 — 여기서 프루닝하지 않는다.
    assert grading.should_prune(50, 2, grounding_min=grounding_min) is False


# --------------------------------------------------------------------------------------
# DND 시간 판정 (`02` 룰 6, `04 §3`)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("local_hour", "expected"),
    [(21, False), (22, True), (23, True), (0, True), (6, True), (7, False), (12, False)],
)
def test_dnd_window_wraps_over_midnight(local_hour: int, expected: bool) -> None:
    """기본값 `22:00 ~ 07:00` 은 자정을 넘는다 — 단순 비교로는 판정할 수 없다."""
    from datetime import UTC, datetime

    now = datetime(2026, 8, 8, local_hour, 30, tzinfo=UTC)
    assert (
        dnd.in_dnd_window(
            now,
            timezone_name="UTC",
            dnd_start=DEFAULT_SETTINGS["dnd_start"],
            dnd_end=DEFAULT_SETTINGS["dnd_end"],
        )
        is expected
    )


def test_dnd_uses_the_answerer_timezone() -> None:
    """판정의 단일 원천은 담당자의 `users.timezone` 이다 (`04 §3`).

    같은 UTC 시각이라도 담당자 타임존이 다르면 결과가 달라진다.
    """
    from datetime import UTC, datetime

    now = datetime(2026, 8, 8, 13, 0, tzinfo=UTC)  # 서울 22:00 / UTC 13:00

    assert (
        dnd.in_dnd_window(now, timezone_name="Asia/Seoul", dnd_start="22:00", dnd_end="07:00")
        is True
    )
    assert dnd.in_dnd_window(now, timezone_name="UTC", dnd_start="22:00", dnd_end="07:00") is False


def test_unknown_timezone_does_not_kill_the_pipeline() -> None:
    """타임존이 깨졌다고 답변을 못 내보내면 안 된다 — UTC 로 판정하고 로그만 남긴다."""
    from datetime import UTC, datetime

    now = datetime(2026, 8, 8, 23, 0, tzinfo=UTC)
    assert (
        dnd.in_dnd_window(now, timezone_name="Mars/Olympus", dnd_start="22:00", dnd_end="07:00")
        is True
    )


def test_away_mode_alone_opens_the_degrade_gate() -> None:
    from datetime import UTC, datetime

    noon = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    assert (
        dnd.should_degrade(
            noon, away_mode=True, timezone_name="UTC", dnd_start="22:00", dnd_end="07:00"
        )
        is True
    )
    assert (
        dnd.should_degrade(
            noon, away_mode=False, timezone_name="UTC", dnd_start="22:00", dnd_end="07:00"
        )
        is False
    )
