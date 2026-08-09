"""검증 질문셋의 단일 원천 (`08 §3` Q1~Q13 + `08 §5` 이력 구성).

세 곳이 같은 표를 쓴다:

- `scripts/seed.py` — `--with-history` 가 던지는 45~60건
- `scripts/eval_questions.py` — 실 LLM 으로 Q1~Q13 을 돌려 기대값과 대조
- `tests/test_demo_scenarios.py` — FakeLLM 회귀 (기대 등급 재현)

⚠️ **질문 문안을 세 곳에 복사하지 않는다.** 복사하면 한쪽만 고쳐진 채로 회귀 테스트가
초록으로 통과하고, 실 LLM 대조는 다른 문장을 재고 있게 된다.

> ### 라이브 시연이 소비하는 질문은 이력에서 뺀다 (`09 §2`)
> - **Q8·Q10 원문**: 시나리오 B 를 라이브로 재현해야 한다. 이력이 미리 던져 두면 데모 당일
>   Q8 이 이미 처리된 질문이 되고, Q10 은 재사용 경로가 이미 소진된 상태가 된다.
>   **변형은 무방하다** — 🔴 `no_evidence` 는 공식 Q&A 를 만들지 않으므로 재사용 경로를
>   오염시키지 않는다.
> - **Q2 원문**: 이력 실행은 허용하되 **승인(`verified`) 금지**다. 승인하면 공식 Q&A 가
>   생기고, 데모 1단계의 Q2 가 재사용 경로로 빠져 "🟢 + 인용"이 화면에서 사라진다
>   (`05 §6` D11).
>
> ⚠️ **1단계 질문이 Q1 → Q2 로 바뀌었다** (사용자 결정 2026-08-09, 배포 세션 실측).
>   배포 DB 실 LLM 대조에서 Q1 은 매칭률 66 으로 🟡 이었고 Q2 는 84 로 🟢 이었다.
>   `08 §4` 1단계는 "근거와 함께 즉답"을 보여 주는 자리라 🟡 로는 성립하지 않는다.
>   그래서 **보호 대상이 Q1 이 아니라 Q2** 다. Q1 은 이제 승인해도 무방하다.
"""

from dataclasses import dataclass

# `04 §2` chunks.meta.heading_path 와 같은 형태 — 청킹이 만드는 값과 1:1 로 맞춘다.
HeadingPath = tuple[str, ...]

GRADE_GREEN = "green"
GRADE_YELLOW = "yellow"
GRADE_RED = "red"


@dataclass(frozen=True)
class DemoQuestion:
    """`08 §3` 검증 질문 1건."""

    key: str
    content_ko: str
    # 기대 등급. Q5·Q9 처럼 **둘 다 정답**인 케이스가 있어 튜플이다 (`08 §3`).
    expected_grades: tuple[str, ...]
    # 기대 근거 청크. `None` 은 "근거가 없는 것이 정답"(강제 🔴)이라는 뜻이다.
    evidence_doc: str | None
    evidence_heading: HeadingPath | None
    note: str = ""


API_SPEC = "api-spec.md"
REFUND_POLICY = "refund-policy.md"
INTEGRATION_GUIDE = "integration-guide.md"
MEETING_NOTES = "meeting-notes-2026-07.md"

_API_SPEC_ROOT = "Orders API Specification v2.1"
_REFUND_ROOT = "Refund Policy v1"
_GUIDE_ROOT = "Integration Guide"
_NOTES_ROOT = "Partner Sync Notes — July 2026"


CANONICAL: tuple[DemoQuestion, ...] = (
    DemoQuestion(
        key="Q1",
        content_ko="주문 조회 API 응답에 user_id 포함되나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "GET /v2/orders/{order_id}"),
        note="데모 1단계. 승인 금지 — 재사용 경로로 빠지면 인용이 사라진다.",
    ),
    DemoQuestion(
        key="Q2",
        content_ko="액세스 토큰 만료 시간이 어떻게 되나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Authentication"),
        note="24시간 + 재인증. M-1 실측 S=88.",
    ),
    DemoQuestion(
        key="Q3",
        content_ko="지원하는 통화가 뭐예요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Currencies"),
        note="M-1 캘리브레이션이 추가한 섹션. 실측 S=56 이라 실 LLM 에서는 🟡 로 떨어질 수 있다.",
    ),
    DemoQuestion(
        key="Q4",
        content_ko="목록 조회 페이지네이션 방식 알려주세요",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Pagination"),
        note="cursor 기반, limit 최대 100. M-1 실측 S=90.",
    ),
    DemoQuestion(
        key="Q5",
        content_ko="웹훅 재시도 정책이 있나요?",
        # `08 §3` 이 🟢~🟡 로 적었다 — 단일 출처라 근거가 얇다.
        expected_grades=(GRADE_GREEN, GRADE_YELLOW),
        evidence_doc=MEETING_NOTES,
        evidence_heading=(_NOTES_ROOT, "API and Rate Limits"),
        note=(
            "재시도 횟수는 meeting-notes 에만 있다. integration-guide 의 "
            "'Retrying Failed API Calls' 는 **클라이언트가 API 를 호출할 때**라 주제가 다르다."
        ),
    ),
    DemoQuestion(
        key="Q6",
        content_ko="배송비도 환불되나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=REFUND_POLICY,
        evidence_heading=(_REFUND_ROOT, "Shipping Fees"),
        note="불량품 제외 환불 불가. M-1 실측 S=100.",
    ),
    DemoQuestion(
        key="Q7",
        content_ko="API 요청 제한이 분당 몇 회인가요?",
        expected_grades=(GRADE_RED,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Rate Limiting"),
        note=(
            "강제 🔴 conflict — api-spec 60/min vs meeting-notes 100/min. 시제·대상·확정성이 "
            "같고 수치만 다르므로 진짜 모순이다. DND 에서도 🔴 을 유지한다 (D2)."
        ),
    ),
    DemoQuestion(
        key="Q8",
        content_ko="환불 정책이 일본 리전에도 동일하게 적용되나요?",
        expected_grades=(GRADE_RED,),
        evidence_doc=None,
        evidence_heading=None,
        note=(
            "강제 🔴 no_evidence — refund-policy 에 일본 조항이 없다. api-spec 의 Regions 는 "
            "`ap-northeast` 가 있다는 인프라 사실만 말하므로 ④ 의 '주제만 언급하는 청크는 "
            "근거가 아니다' 규칙에 걸린다 (`06 §2` ④)."
        ),
    ),
    DemoQuestion(
        key="Q9",
        content_ko="결제 게이트웨이는 어떤 PG사를 쓰나요?",
        # `08 §3` — 미끼 청크가 검색은 되므로 `no_evidence` 도 `low_confidence` 도 정답이다.
        expected_grades=(GRADE_RED,),
        evidence_doc=None,
        evidence_heading=None,
        note=(
            "미끼: meeting-notes 의 'Payment gateway integration is on track.' 은 어휘만 겹치고 "
            "PG 사 이름이 없다. `low_confidence` 로 떨어지면 담당자 DND 시간대에 🟡 로 강등될 "
            "수 있으므로(D2) 회귀 테스트에서는 `not_answerable` 로 고정한다 (`09 §3`)."
        ),
    ),
    DemoQuestion(
        key="Q10",
        content_ko="일본 리전 환불 정책도 동일하게 적용되나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=None,
        evidence_heading=None,
        note=(
            "Q8 확정 후 공식 Q&A 재사용. 어휘 근접은 원시 코사인을 `reuse_threshold` 위로 "
            "올리는 보조 수단일 뿐이고 최종 판정은 ② 2차 게이트(yes/no)다 (`08 §3`)."
        ),
    ),
    DemoQuestion(
        key="Q11",
        content_ko="웹훅 서명은 어떻게 검증하나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=INTEGRATION_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Webhook Signature Verification"),
        note="HMAC-SHA256, 5분 윈도우. 확장 시드 섹션이 top-k 에 들어오는지 보는 케이스.",
    ),
    DemoQuestion(
        key="Q12",
        content_ko="멱등키는 얼마나 유지되나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Idempotency"),
        note="24시간, 같은 키·다른 body 는 409. M-1 실측 S=66.",
    ),
    DemoQuestion(
        key="Q13",
        content_ko="샌드박스에서 실제 결제가 발생하나요?",
        expected_grades=(GRADE_GREEN,),
        evidence_doc=API_SPEC,
        evidence_heading=(_API_SPEC_ROOT, "Sandbox"),
        note="실제 결제 없음, 테스트 카드만, 매주 일요일 리셋. M-1 실측 S=91.",
    ),
)

BY_KEY: dict[str, DemoQuestion] = {question.key: question for question in CANONICAL}

# 라이브 시연이 그대로 던지는 질문 — **원문을 이력에 소진하지 않는다** (`09 §2`).
LIVE_ONLY_KEYS: frozenset[str] = frozenset({"Q8", "Q10"})

# 승인(`verified`) 주입 제외 — 공식 Q&A 가 생기면 데모 당일 재사용 경로로 빠진다 (`09 §2`).
#
# ⚠️ **Q1 이 아니라 Q2 다** (사용자 결정 2026-08-09). `08 §4` 1단계 질문이 Q2 로 바뀌었으므로
#    보호 대상도 함께 옮긴다. 여기를 안 옮기면 Q2 계열이 승인돼 공식 Q&A 가 생기고,
#    데모 당일 1단계가 "즉답 + 인용" 대신 **재사용**으로 빠져 인용 화면이 사라진다.
NO_APPROVAL_KEYS: frozenset[str] = frozenset({"Q2", "Q8", "Q10"})


@dataclass(frozen=True)
class HistoryQuestion:
    """`--with-history` 가 던지는 1건. `family` 는 어느 canonical 질문의 계열인가다."""

    family: str
    content_ko: str
    verbatim: bool = False

    @property
    def approvable(self) -> bool:
        """승인(`verified`) 주입 대상인가.

        **계열 전체를 막는다** — 원문만 막고 변형을 승인하면 그 변형이 공식 Q&A 가 되고,
        데모 당일 원문이 그 Q&A 에 걸려 재사용된다. `09 §2` 의 "변형 시드 목록"이 이것이다.
        """
        return self.family not in NO_APPROVAL_KEYS


def _family(key: str, *paraphrases: str) -> tuple[HistoryQuestion, ...]:
    """원문 + 패러프레이즈. 원문이 `LIVE_ONLY_KEYS` 면 변형만 남는다."""
    items: list[HistoryQuestion] = []
    if key not in LIVE_ONLY_KEYS:
        items.append(HistoryQuestion(family=key, content_ko=BY_KEY[key].content_ko, verbatim=True))
    items.extend(HistoryQuestion(family=key, content_ko=text) for text in paraphrases)
    return tuple(items)


# --------------------------------------------------------------------------------------
# 이력 질문셋 (`08 §5` 3번 · `09 §2`)
#
# 총 58건. 🟢 기대(Q1~Q6·Q11~Q13 계열) 49건 · 🔴 기대(Q7~Q9 계열) 9건.
#
# ⚠️ **M-1 실측 S 가 높은 계열에 변형을 몰아 두었다** (Q2 88 · Q4 90 · Q6 100 · Q11 91 ·
#    Q13 91). Q1(65)·Q3(56)·Q5(54)·Q12(66) 는 정답 청크를 top-1 으로 잡고도 원시 유사도가
#    중간대라 매칭률이 S 에 막혀 🟡 이 될 수 있다 (`08 §3` M-1 대조 결과). 그 넷에 변형을
#    많이 배치하면 🟢 표본이 30건에 못 미쳐 `grade_accuracy` 가 통째로 "표본 부족"이 된다
#    (D25) — 발표 마지막 화면이 비어 보이는 실패 모드가 바로 이것이다.
# --------------------------------------------------------------------------------------
HISTORY: tuple[HistoryQuestion, ...] = (
    *_family(
        "Q2",
        "토큰은 몇 시간 뒤에 만료되나요?",
        "액세스 토큰 유효기간 알려주세요.",
        "인증 토큰이 만료되면 어떻게 해야 하나요?",
        "리프레시 토큰도 발급되나요?",
        "토큰 만료 후 재인증이 필요한가요?",
        "Bearer 토큰은 얼마나 오래 쓸 수 있나요?",
        "액세스 토큰 수명이 24시간이 맞나요?",
    ),
    *_family(
        "Q4",
        "리스트 API는 어떤 방식으로 페이징하나요?",
        "커서 기반 페이지네이션을 쓰나요?",
        "한 번에 최대 몇 건까지 받을 수 있나요?",
        "limit 파라미터 최대값이 얼마인가요?",
        "목록 API에 cursor 파라미터가 있나요?",
        "페이지 번호로 조회할 수 있나요?",
        "다음 페이지는 어떻게 요청하나요?",
    ),
    *_family(
        "Q6",
        "반품할 때 배송비는 돌려받을 수 있나요?",
        "불량품이면 배송비도 환불해 주나요?",
        "단순 변심 반품의 배송비는 누가 부담하나요?",
        "왕복 배송비 정책이 어떻게 되나요?",
        "빠른 배송 추가금도 환불 대상인가요?",
        "반품 라벨 비용은 고객이 내나요?",
        "배송비 환불에 예외가 있나요?",
    ),
    *_family(
        "Q11",
        "X-GlobalMart-Signature 헤더는 어떻게 확인하나요?",
        "웹훅 서명 검증에 어떤 해시를 쓰나요?",
        "서명 타임스탬프 허용 오차가 얼마인가요?",
        "웹훅 리플레이 공격은 어떻게 막나요?",
        "웹훅 본문을 파싱한 뒤에 서명을 검증해도 되나요?",
        "HMAC 서명 계산 방법을 알려주세요.",
        "웹훅 시크릿은 어디에 쓰나요?",
    ),
    *_family(
        "Q13",
        "테스트 환경에서 진짜 돈이 나가나요?",
        "샌드박스에서 쓸 수 있는 카드는 어떤 건가요?",
        "샌드박스 데이터는 언제 초기화되나요?",
        "샌드박스 호스트 주소가 어떻게 되나요?",
        "테스트 키를 운영 환경에 써도 되나요?",
        "샌드박스에서도 웹훅이 오나요?",
        "샌드박스 요청 제한은 운영과 다른가요?",
    ),
    # 🟢 기대이지만 M-1 실측 S 가 중간대인 계열 — 변형을 적게 둔다.
    *_family(
        "Q1",
        "주문 상세 응답에 구매자 계정 아이디가 들어 있나요?",
        "GET 주문 API가 user_id 필드를 돌려주나요?",
    ),
    *_family("Q3", "결제 가능한 통화 종류를 알려주세요."),
    *_family("Q5", "웹훅 전송이 실패하면 몇 번 재시도하나요?"),
    *_family("Q12", "Idempotency-Key 보관 기간이 어떻게 되나요?"),
    # 🔴 계열 — 등급 분포 확인용 9건 (`09 §2` "8~10건에 그친다").
    *_family(
        "Q7",
        "분당 호출 한도가 60회인가요 100회인가요?",
        "레이트 리밋 수치가 문서마다 다른데 어느 게 맞나요?",
    ),
    # Q8 은 원문을 빼고 변형만 던진다 (`_family` 가 `LIVE_ONLY_KEYS` 로 처리한다).
    *_family(
        "Q8",
        "일본에서도 30일 환불 규정이 그대로 적용되나요?",
        "JP 리전 고객의 환불 기한은 며칠인가요?",
        "일본 소비자법에 맞춘 환불 규정이 따로 있나요?",
    ),
    *_family(
        "Q9",
        "결제 대행사 이름이 뭔가요?",
        "PG 연동은 어디랑 되어 있나요?",
    ),
)

# 🟢 기대 계열 (`08 §3` 표에서 🟢 인 것). 등급 분포 보고에 쓴다.
GREEN_EXPECTED_FAMILIES: frozenset[str] = frozenset(
    {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q11", "Q12", "Q13"}
)
