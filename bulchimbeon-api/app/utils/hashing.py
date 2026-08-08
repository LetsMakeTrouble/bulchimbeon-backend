"""교훈 `content_hash` 정규화 — **명문화된 정의의 단일 구현** (`03 §5.2`, `06 §3`).

> ### 이 정의를 벗어난 해시 계산을 만들지 않는다 (`03 §5.2`)
> `content_hash` 는 저장용 지문이 아니라 **삭제 교훈 재생성 차단(D8)의 판정 키**다.
> 후보를 만들기 전에 `status='deleted'` 행의 해시와 대조해 같으면 생성을 스킵하므로,
> 계산식이 두 곳으로 갈라지는 순간 "지운 교훈이 다시 올라온다"가 조용히 부활한다.
> 그래서 정규화는 이 함수 하나만 알고 있고, 호출부는 문자열을 그대로 넘긴다.

⚠️ **CRLF 정규화는 `.gitattributes` 의 `*.md text eol=lf`(M0)와 짝이다.**
교훈은 문서에서 파생된 문장을 담는데, 줄바꿈이 OS 마다 다르게 저장되면 같은 원칙이
Windows 와 Linux 에서 **다른 해시**를 낸다. 그러면 대조가 항상 빗나가 D8 이 무력화되고,
담당자는 이미 지운 교훈을 영원히 다시 판단하게 된다. 리포지터리 쪽(`.gitattributes`)과
런타임 쪽(이 함수)을 둘 다 걸어야 양쪽 경로가 막힌다.
"""

import hashlib
import re

# 탭·개행·연속 스페이스를 한 칸으로 접는다. `\s` 라서 CRLF 를 LF 로 바꾼 뒤의 `\n` 도 함께 먹는다.
_WHITESPACE_RUN = re.compile(r"\s+")


def lesson_content_hash(lesson: str) -> str:
    """교훈 한 줄을 정규화해 sha256 16진 문자열로 만든다 (`03 §5.2`, `06 §3` 작업 4).

    순서는 **strip → lower → CRLF 정규화 → 연속 공백 단일화**로 고정이다. 앞뒤 공백·대소문자·
    줄바꿈 표기·중간 공백 폭이 달라도 같은 원칙이면 같은 해시가 나와야 한다 — 그래야
    `"Japan is 20 days.\\r\\n"` 과 `"  japan  is 20 days. "` 가 재생성 차단(D8)에서
    한 건으로 묶인다.
    """
    normalized = _WHITESPACE_RUN.sub(" ", lesson.replace("\r\n", "\n").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
