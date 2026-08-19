"""데모 프로필 — **어느 코퍼스·질문셋으로 시드할 것인가** 하나를 고르는 자리.

두 데모가 나란히 있다.

| 프로필 | 프로젝트 | 코퍼스 | 언어 |
| --- | --- | --- | --- |
| `globalmart` (기본) | GlobalMart JP Launch | `seed/*.md` 4개 | 전부 영어 |
| `bulchimbeon` | Bulchimbeon Platform | `seed/bulchimbeon/*.md` 5개 | 프론트 영어 · 백엔드 한국어 |

⛔ **기본값을 바꾸지 마라.** `globalmart` 는 M-1 게이트에서 임계값을 실측한 코퍼스이고
(`08 §1`), 회귀 테스트·`eval_questions.py`·발표 대본이 전부 그 질문셋을 가리킨다.
불침번 프로필은 **더하는 것**이지 대체하는 것이 아니다.

> ### 두 프로젝트는 서로를 밀어내지 않는다
> `projects.settings` 도 `daily_llm_call_limit` 집계도 **프로젝트별**이다
> (`quota.used` 가 `LLMUsage.project_id` 로 센다). 그래서 불침번 프로젝트의 임계값을
> 조정해도 GlobalMart 의 실측값은 그대로고, 한쪽 이력을 채워도 다른 쪽 상한을 먹지 않는다.

> ### 새 프로필을 더할 때
> 1. `seed/<이름>/` 에 코퍼스를 넣는다. **H1 바로 아래에 본문을 두지 마라** — 헤딩만 있는
>    섹션은 청킹이 버리므로(`utils/chunking._has_prose`) `##` 섹션 하나가 청크 하나가 된다.
> 2. 질문셋 모듈을 만든다 (`bulchimbeon_questions.py` 를 본떠서).
> 3. 여기에 `DemoProfile` 을 추가한다.
> 4. **`expected_chunks` 를 실제로 세어서 적는다.** 이 값이 틀리면 시드가 어서션에서 멈춘다.
>    청크 수가 `retrieval_top_k` 이하로 떨어지면 검색이 "전부 반환"으로 퇴화하는데,
>    그래도 파이프라인은 초록으로 돌기 때문에 어서션 없이는 조용히 깨진다.
"""

from dataclasses import dataclass, field
from pathlib import Path

from scripts import bulchimbeon_questions, demo_questions

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DemoUser:
    email: str
    name: str
    language: str
    timezone: str


@dataclass(frozen=True)
class DemoProfile:
    """시드 한 번이 만드는 세계. `scripts/seed.py --profile <key>` 가 고른다."""

    key: str
    project_name: str
    project_description: str
    guidelines: str
    answerer: DemoUser
    askers: tuple[DemoUser, ...]
    seed_dir: Path
    # (파일명, 제목). 제목은 문서 본문의 H1 을 그대로 쓴다.
    documents: tuple[tuple[str, str], ...]
    expected_chunks: int
    canonical: tuple[demo_questions.DemoQuestion, ...]
    by_key: dict[str, demo_questions.DemoQuestion]
    # `HistoryQuestion` 은 질문셋 모듈마다 따로 정의된다 (`bulchimbeon_questions` 독스트링).
    # 시드는 `family` 와 `content_ko` 만 읽으므로 타입을 하나로 묶지 않는다.
    history: tuple[object, ...]
    no_approval_keys: frozenset[str]
    no_red_resolve_keys: frozenset[str]
    # 라이브 시연이 **원문 그대로** 던지는 계열. 이력은 변형만 던진다.
    #
    # ⚠️ `no_approval_keys` 와 다른 축이다. 승인 제외는 "계열 전체를 공식 Q&A 로 만들지
    #    않는다" 이고, 이쪽은 "원문 문장을 이력이 소진하지 않는다" 다. GlobalMart 의 Q2·Q7 은
    #    앞의 것만 해당하므로 **원문이 이력에 들어가는 것이 정상**이다.
    live_only_keys: frozenset[str]
    # 이 프로필만의 `projects.settings` 조정. 비어 있으면 기본값 그대로다.
    #
    # ⚠️ 여기에 키를 넣으면 시드의 기본값 어서션이 **그 키만** 건너뛴다. 교차언어 코퍼스라
    #    `similarity_floor` 를 낮춰야 하는 경우가 이 필드의 존재 이유다 — 낮추기 전에
    #    `probe_seed_docs.py` 로 실제 S 분포를 먼저 재라.
    settings_overrides: dict[str, float] = field(default_factory=dict)
    # 시드가 끝난 프로젝트의 **관리자로 앉힐 계정**. 이 도메인의 관리자는 담당자(answerer)다 —
    # 역할 enum 에 admin 이 따로 없고(`04 §2` — `role IN ('answerer','asker')`),
    # 전체 질문·카드 큐·설정을 보는 쪽은 담당자뿐이다.
    #
    # ⚠️ `answerer` 를 직접 바꾸지 않고 이 필드를 쓰는 이유: `answerer` 는 이력의 주인공이다.
    #    픽스처(`seed/<프로필>/history.json`)의 카드 확정·알림·이벤트가 전부 그 이메일을
    #    가리키므로, 계정을 갈아치우면 픽스처가 "픽스처가 가리키는 유저가 없다" 로 죽는다.
    #    대신 시드 마지막에 이 계정을 멤버로 넣고 `transfer_answerer`(D16) 로 담당자를 넘긴다 —
    #    구담당자는 질문자로 남아 이력이 그대로 이어진다.
    admin: DemoUser | None = None

    @property
    def canonical_texts(self) -> frozenset[str]:
        """공식 Q&A 가 되면 안 되는 질문 원문 — 검증·라이브 경로 보호용."""
        return frozenset(question.content_ko for question in self.canonical)


GLOBALMART = DemoProfile(
    key="globalmart",
    project_name="GlobalMart JP Launch",
    project_description=(
        "한국 커머스팀이 미국 개발 파트너(DevCorp)의 API로 일본 리전 런칭을 준비하는 프로젝트."
    ),
    guidelines=(
        "Answers must reference the exact API version. If a policy differs by region, always "
        "say which regions were checked. Prefer concise answers with field names in backticks."
    ),
    answerer=DemoUser("mike@devcorp.example", "Mike Chen", "en", "America/New_York"),
    askers=(
        DemoUser("jisoo@globalmart.example", "지수", "ko", "Asia/Seoul"),
        DemoUser("minjun@globalmart.example", "민준", "ko", "Asia/Seoul"),
    ),
    seed_dir=PROJECT_ROOT / "seed",
    documents=(
        ("api-spec.md", "Orders API Specification v2.1"),
        ("refund-policy.md", "Refund Policy v1"),
        ("integration-guide.md", "Integration Guide"),
        ("meeting-notes-2026-07.md", "Partner Sync Notes — July 2026"),
    ),
    # ⚠️ 23 → 95. 청킹 2차 분할(`MAX_UNITS_PER_CHUNK`)이 들어오면서 섹션 하나가 문장
    #    3개씩 창으로 나뉜다. **M-1 캘리브레이션은 23청크 시절 값이므로** 임계값이 여전히
    #    맞는지는 `eval_questions.py` 로 확인한다.
    expected_chunks=95,
    canonical=demo_questions.CANONICAL,
    by_key=demo_questions.BY_KEY,
    history=demo_questions.HISTORY,
    no_approval_keys=demo_questions.NO_APPROVAL_KEYS,
    no_red_resolve_keys=demo_questions.NO_RED_RESOLVE_KEYS,
    live_only_keys=demo_questions.LIVE_ONLY_KEYS,
)

# 불침번 팀 자신을 지식으로 삼는 데모.
#
# 담당자는 프론트를 맡은 해외 개발자(영어), 질문자는 한국 팀이다. 그래서 **프론트 문서는
# 영어, 백엔드·인프라 문서는 한국어**이고 — 번역 서사가 데모용 설정이 아니라 이 저장소의
# 실제 사정이 된다. 교차언어 충돌(Q13: 토큰 수명 30분 vs 60분)이 그 사정에서 자연스럽게
# 나오는 장면이다.
BULCHIMBEON = DemoProfile(
    key="bulchimbeon",
    project_name="Bulchimbeon Platform",
    project_description=(
        "불침번 자체 개발 프로젝트. 프론트엔드 문서는 영어, 백엔드·인프라 문서는 한국어로 "
        "쌓여 있고 질문은 한국어로 들어온다."
    ),
    guidelines=(
        "Answers must name the exact route, settings key, or environment variable involved. "
        "When the frontend and backend documents disagree, do not pick one — hand it over. "
        "Prefer concise answers with identifiers in backticks."
    ),
    answerer=DemoUser("alex@bulchimbeon.example", "Alex Rivera", "en", "America/Los_Angeles"),
    # 질문자는 GlobalMart 와 같은 계정을 쓴다 — 로그인 안내가 늘어나지 않고,
    # 로그인하면 프로젝트 목록에 두 데모가 함께 보인다.
    askers=GLOBALMART.askers,
    seed_dir=PROJECT_ROOT / "seed" / "bulchimbeon",
    documents=(
        ("frontend-guide.md", "Bulchimbeon Web Client Guide"),
        ("frontend-release-notes.md", "Web Client Release Notes"),
        ("backend-pipeline.md", "답변 파이프라인"),
        ("backend-rules.md", "운영 규칙"),
        ("infra-deploy.md", "배포 구성"),
    ),
    # 실측값 — `##` 섹션 7+3+6+6+4. 청킹 코드로 직접 세었다 (2026-08-16).
    #
    # ⚠️ 토큰 수명은 영어·한국어 문서 **양쪽에서 독립 섹션**이다. 큰 섹션 안에 한 문장으로
    #    묻혀 있으면 청크가 희석돼 Q13(교차언어 충돌)이 `similarity_floor` 아래로 내려가고,
    #    강제 🔴 사유가 `conflict` 가 아니라 `no_evidence` 가 된다 — 충돌 시연이 사라진다.
    expected_chunks=108,
    canonical=bulchimbeon_questions.CANONICAL,
    by_key=bulchimbeon_questions.BY_KEY,
    history=bulchimbeon_questions.HISTORY,
    no_approval_keys=bulchimbeon_questions.NO_APPROVAL_KEYS,
    no_red_resolve_keys=bulchimbeon_questions.NO_RED_RESOLVE_KEYS,
    live_only_keys=bulchimbeon_questions.LIVE_ONLY_KEYS,
    # ⚠️ 임계값은 조정하지 않는다 — **기본값 그대로**다.
    #
    # 상한만 올린다. 언어별 검색 축이 들어오면서 질문당 임베딩이 하나 늘어 실측 5~6 호출이
    # 됐고(① 번역 1 + 임베딩 2 + ④ + ⑤ + ⑦), 이력 109건이면 기본 상한 500 을 넘는다.
    # 실제로 지난 실행에서 뒷부분 8건이 조용히 강제 🔴 `quota_exceeded` 로 떨어졌다.
    # ⛔ 이 값은 **시드가 쓰는 한도**다. 발표 당일 라이브 질문은 API 서버 프로세스에서
    #    별도로 세므로 여기 올린다고 라이브가 위험해지지 않는다.
    settings_overrides={"daily_llm_call_limit": 1000},
    # 데모 관리자 계정(mike)이 이 프로젝트도 담당자로 관리한다 — 로그인 하나로 두 데모의
    # 관리자 화면(전체 질문 115건 + 작성자, 카드 인박스)을 모두 보여 주기 위해서다.
    # Alex 는 질문자로 남는다 (`ensure_admin` — 픽스처의 이력 주인이라 유저를 지우면 안 된다).
    admin=GLOBALMART.answerer,
    #
    # 한때 여기에 `similarity_floor` 0.325 를 넣었다. 한국어 문서를 영어 질의로만 찾던
    # 시절의 보정이었는데, 검색이 언어별 축으로 바뀌면서(0013 · `retrieval.search_evidence`)
    # 그 보정의 이유가 사라졌다. 같은 언어끼리 재면 유사도가 원래 대역으로 돌아온다.
    #
    # ⛔ 여기에 값을 다시 넣기 전에 `probe_seed_docs.py --profile bulchimbeon` 을 먼저 돌려라.
    #    임계값을 낮추는 것은 검색이 실제로 못 찾을 때의 마지막 수단이다.
)

PROFILES: dict[str, DemoProfile] = {
    GLOBALMART.key: GLOBALMART,
    BULCHIMBEON.key: BULCHIMBEON,
}

DEFAULT_PROFILE = GLOBALMART


def get(key: str) -> DemoProfile:
    try:
        return PROFILES[key]
    except KeyError:
        raise SystemExit(
            f"모르는 프로필: {key} — 가능한 값: {', '.join(sorted(PROFILES))}"
        ) from None
