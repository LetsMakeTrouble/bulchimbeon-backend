"""DND(방해 금지 시간) 판정 (`02` 룰 6, D2, `06 §2` ⑥).

> ### 타임존 단일 원천
> 판정 기준은 **항상 현재 담당자(`projects.answerer_id`)의 `users.timezone`** 이다.
> `projects.settings` 에 타임존 키를 두지 않는다 — **`briefing_timezone` 은 존재하지 않는다**
> (`04 §3`). 담당자가 교체되면 판정 기준도 자동으로 따라간다.

> ### 강등 대상은 `low_confidence` 뿐이다
> 강제 🔴 4종(`conflict`/`no_evidence`/`schema_failed`/`quota_exceeded`)은 **DND 에서도 🔴 을
> 유지한다.** 충돌 감지 결과나 근거 없음이 "몇 시에 물었는가"에 따라 조용히 은폐되는 것은
> 룰 2 위반보다 심각하다. 이 규칙 덕분에 데모 Q7·Q8 이 **시연 시각과 무관하게 🔴** 로 재현된다.
"""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_FALLBACK_TIMEZONE = "UTC"


def _parse_hhmm(value: str) -> time | None:
    """`"22:00"` → `time(22, 0)`. 형식이 깨졌으면 None (판정을 건너뛴다)."""
    try:
        hour, _, minute = value.partition(":")
        return time(int(hour), int(minute))
    except (AttributeError, ValueError):
        logger.warning("dnd 시각 형식이 올바르지 않다: %r", value)
        return None


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # 타임존이 깨졌다고 파이프라인을 죽이지 않는다. UTC 로 판정하고 로그만 남긴다.
        logger.warning("알 수 없는 timezone=%r — UTC 로 판정한다", timezone_name)
        return ZoneInfo(_FALLBACK_TIMEZONE)


def in_dnd_window(now: datetime, *, timezone_name: str, dnd_start: str, dnd_end: str) -> bool:
    """담당자 현지 시각이 DND 구간 안인가.

    `22:00 ~ 07:00` 처럼 **자정을 넘는 구간**이 기본값이므로 단순 비교로는 안 된다
    (`04 §3`). start > end 이면 두 조각(`start~24:00`, `00:00~end`)의 합집합이다.
    """
    start = _parse_hhmm(dnd_start)
    end = _parse_hhmm(dnd_end)
    if start is None or end is None or start == end:
        return False

    local = now.astimezone(_zone(timezone_name)).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end


def should_degrade(
    now: datetime,
    *,
    away_mode: bool,
    timezone_name: str,
    dnd_start: str,
    dnd_end: str,
) -> bool:
    """🔴 → 🟡 강등 **후보** 여부. 실제 강등은 호출자가 `held_reason` 을 함께 본다.

    ⚠️ `away_mode`(퇴근 모드)도 포함한다. `05 §6` 이 "강제 🔴 4종은 **퇴근 모드/DND
    시간대**에도 🔴 을 유지한다"라고 적어 둔 것은, 그 둘이 같은 강등 관문을 공유한다는 뜻이다.
    담당자가 자리에 없다는 사실은 시각으로 표현되든 스위치로 표현되든 같은 사건이다.
    """
    if away_mode:
        return True
    return in_dnd_window(now, timezone_name=timezone_name, dnd_start=dnd_start, dnd_end=dnd_end)


def next_dnd_end_at(
    now: datetime, *, timezone_name: str, dnd_start: str, dnd_end: str
) -> datetime | None:
    """DND 종료 시각(UTC). **DND 밖이면 `None`** — 보류할 이유가 없다는 뜻이다.

    룰 6 은 "담당자의 방해 금지 시간에는 **어떤 즉시 알림도** 보내지 않는다"라고 못박는다.
    긴급 카드 알림조차 이 구간에서는 `notifications.deliver_after` 를 여기가 돌려주는 시각으로
    잡아 보류한다 (`05 §11` 목록·카운트가 `deliver_after <= now()` 만 노출한다).

    ⚠️ `away_mode`(퇴근 모드)는 여기서 보지 않는다. 강등 판정(`should_degrade`)과 달리
    보류는 **끝나는 시각이 필요**한데 퇴근 모드에는 그것이 없고, 룰 6 이 알림 보류의 기준으로
    지목한 것은 DND 시간대뿐이다.
    """
    if not in_dnd_window(now, timezone_name=timezone_name, dnd_start=dnd_start, dnd_end=dnd_end):
        return None

    end = _parse_hhmm(dnd_end)
    if end is None:  # in_dnd_window 가 이미 걸렀지만 타입을 좁힌다.
        return None

    zone = _zone(timezone_name)
    local = now.astimezone(zone)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        # 자정을 넘는 구간(`22:00~07:00`)에서 아직 자정 전이면 종료는 내일이다.
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_briefing_at(now: datetime, *, timezone_name: str, briefing_hour: int) -> datetime:
    """다음 브리핑 시각(UTC) — `defer` 의 기본 만기다 (D15, `05 §7.3`).

    타임존 판정이 DND 와 **같은 원천**(담당자 `users.timezone`)이라 여기 둔다. M6 의 브리핑
    스케줄러도 이 함수를 쓴다 — 두 곳에서 따로 계산하면 담당자 교체 시 어긋난다.

    이미 오늘 브리핑 시각을 지났으면 내일 같은 시각이다.
    """
    zone = _zone(timezone_name)
    local = now.astimezone(zone)
    hour = min(23, max(0, int(briefing_hour)))
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
