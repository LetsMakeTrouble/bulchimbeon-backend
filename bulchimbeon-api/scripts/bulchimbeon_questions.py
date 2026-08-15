"""불침번 자체 데모(`seed/bulchimbeon/`)의 질문셋.

`demo_questions.py` 와 같은 모양이지만 **다른 코퍼스**를 가리킨다. 두 데모는 나란히 존재하고,
어느 쪽을 시드할지는 `scripts/demo_profiles.py` 의 프로필이 고른다.

> ### 코퍼스가 두 언어로 섞여 있다
> 프론트 문서는 영어, 백엔드·인프라 문서는 한국어다. 파이프라인 ①이 한국어 질문을 영어로
> 번역해 임베딩하므로 **영어 문서 쪽이 검색 축과 같은 언어**이고, 한국어 문서는 교차언어
> 매칭이 된다. 그래서 계열별 기대가 다르다.
>
> ⚠️ **한국어 근거 계열(Q7~Q12)의 기대 등급은 🟢🟡 둘 다 정답이다.** 교차언어 코사인이
> 같은 뜻이어도 동일 언어보다 낮게 나오는 것이 정상이고, 그 대역에서 매칭률은 S 에 막혀
> 🟡 이 될 수 있다. 여기서 🟢 만 기대하면 발표 전날 밤에 **없는 문제**를 쫓게 된다.
>
> ⛔ **시드 전에 `probe_seed_docs.py --profile bulchimbeon` 으로 S 를 먼저 재라.** 한국어
> 계열이 `similarity_floor` 아래로 내려가면 전부 강제 🔴 `no_evidence` 가 되어 이력이
> 통째로 무의미해진다. 그때는 질문 문안에 주제 앵커를 넣거나, **이 프로젝트의**
> `similarity_floor` 만 낮춘다 (`projects.settings` 는 프로젝트별이다).

> ### 라이브 시연이 쓰는 질문은 이력에서 뺀다
> - **Q13·Q14 원문**: 충돌(🔴 `conflict`)과 근거 없음(🔴 `no_evidence`)을 발표장에서 그대로
>   재현해야 한다. 이력이 먼저 던지면 당일에는 이미 처리된 질문이 된다. 변형은 무방하다 —
>   강제 🔴 은 공식 Q&A 를 만들지 않으므로 재사용 경로를 오염시키지 않는다.
> - **Q2 계열 승인 금지**: 1단계 "근거와 함께 즉답"을 보여 줄 질문이다. 승인되면 공식 Q&A 가
>   생기고, 당일 같은 질문이 재사용 경로로 빠져 매칭률·인용이 전부 사라진다.
"""

from dataclasses import dataclass

from scripts.demo_questions import (
    GRADE_GREEN,
    GRADE_RED,
    GRADE_YELLOW,
    DemoQuestion,
)

FRONTEND_GUIDE = "frontend-guide.md"
RELEASE_NOTES = "frontend-release-notes.md"
BACKEND_PIPELINE = "backend-pipeline.md"
BACKEND_RULES = "backend-rules.md"
INFRA_DEPLOY = "infra-deploy.md"

_GUIDE_ROOT = "Bulchimbeon Web Client Guide"
_NOTES_ROOT = "Web Client Release Notes"
_PIPELINE_ROOT = "답변 파이프라인"
_RULES_ROOT = "운영 규칙"
_DEPLOY_ROOT = "배포 구성"

# 기대 등급은 **언어가 아니라 실측**으로 나눈다 (`probe_seed_docs.py --profile bulchimbeon`,
# 2026-08-16). 한국어 섹션에 영어 요약을 단 뒤로는 언어보다 섹션의 밀도가 더 크게 작동한다 —
# Q4(영어 근거)가 S 94 인데 Q3·Q6(같은 영어 문서)은 72·73 이다.
#
# ⚠️ 여기를 실측과 어긋나게 두면 `report_grades` 가 매번 "기대 등급 밖" 을 찍는다.
# 발표 전날 밤에 **없는 문제**를 쫓게 되는 자리다.
# 실측(2026-08-16, 언어별 검색 축 · 기본 임계값): S 92~100 이 _GREEN, 59~78 이 _MIXED 다.
# ⚠️ Q1 은 78 로 초록 하한(80) 바로 아래다 — 🟡 이 나오는 것이 사양대로다.
_GREEN = (GRADE_GREEN,)
_MIXED = (GRADE_GREEN, GRADE_YELLOW)


CANONICAL: tuple[DemoQuestion, ...] = (
    DemoQuestion(
        key="Q1",
        content_ko="SSE 스트림이 끊기면 클라이언트가 몇 초 뒤에 다시 붙나요?",
        expected_grades=_MIXED,
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Live Updates over SSE"),
        note="3초. 릴리스 노트 v0.5 도 같은 값이라 두 근거가 일치한다 — 충돌이 아니다.",
    ),
    DemoQuestion(
        key="Q2",
        content_ko="VITE_API_BASE_URL 을 바꾸면 컨테이너만 다시 시작해도 반영되나요?",
        expected_grades=_GREEN,
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Build-time Configuration"),
        note="아니다 — 빌드 시각에 번들로 박히므로 이미지를 다시 구워야 한다. 라이브 1단계 질문.",
    ),
    DemoQuestion(
        key="Q3",
        content_ko="assets 아래 번들 파일은 캐시를 어떻게 설정하나요?",
        expected_grades=_MIXED,
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Static Serving and Caching"),
        note="파일명에 콘텐츠 해시가 있어 1년 immutable. index.html 은 no-cache 로 대비된다.",
    ),
    DemoQuestion(
        key="Q4",
        content_ko="담당자 확인 카드 인박스 화면의 경로가 무엇인가요?",
        expected_grades=_GREEN,
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Routes and Screens"),
        note="`/inbox`. 경로 목록이 한 청크에 모여 있어 S 가 높게 나오는 계열이다.",
    ),
    DemoQuestion(
        key="Q5",
        content_ko="노란색 등급 답변은 질문자 화면에 어떻게 표시되나요?",
        expected_grades=_GREEN,
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Grade Badges and Cross-check Controls"),
        note="답변은 주되 '담당자 확인 대기' 뱃지가 붙는다.",
    ),
    DemoQuestion(
        key="Q6",
        content_ko="문서의 새 버전을 활성화하면 이전 버전은 어떻게 되나요?",
        expected_grades=_MIXED,
        evidence_doc=RELEASE_NOTES,
        evidence_heading=(_NOTES_ROOT, "v0.3 — Document Shelf"),
        note="대체됨으로 표시되지만 계속 읽을 수 있다 — 옛 인용이 깨지지 않는다.",
    ),
    DemoQuestion(
        key="Q7",
        content_ko="답변의 매칭률은 검색 점수와 근거 충족도를 어떻게 조합해 계산하나요?",
        expected_grades=_MIXED,
        evidence_doc=BACKEND_PIPELINE,
        evidence_heading=(_PIPELINE_ROOT, "매칭률 계산"),
        note="min(S, G) — 평균이 아니다. 한국어 근거라 🟡 도 사양대로다.",
    ),
    DemoQuestion(
        key="Q8",
        content_ko="매칭률과 무관하게 무조건 보류로 떨어지는 조건에는 어떤 것들이 있나요?",
        expected_grades=_MIXED,
        evidence_doc=BACKEND_PIPELINE,
        evidence_heading=(_PIPELINE_ROOT, "강제 보류 네 가지"),
        note="conflict · no_evidence · schema_failed · quota_exceeded 넷. 방해금지에서도 유지된다.",
    ),
    DemoQuestion(
        key="Q9",
        content_ko="확정된 공식 Q&A 를 재사용할 때 한국어 원문을 다시 번역하나요?",
        expected_grades=_GREEN,
        evidence_doc=BACKEND_PIPELINE,
        evidence_heading=(_PIPELINE_ROOT, "재사용 판정"),
        note="재번역하지 않는다 — 승인된 문장이 매번 달라지면 안 되기 때문이다.",
    ),
    DemoQuestion(
        key="Q10",
        content_ko="방해금지 시간과 브리핑 시각은 누구의 타임존을 기준으로 판정하나요?",
        expected_grades=_MIXED,
        evidence_doc=BACKEND_RULES,
        evidence_heading=(_RULES_ROOT, "알림과 방해금지"),
        note="담당자 계정의 타임존. 프로젝트 설정에 타임존 키는 없다.",
    ),
    DemoQuestion(
        key="Q11",
        content_ko="앞단 터널은 /api 로 시작하는 요청을 어디로 보내나요?",
        expected_grades=_MIXED,
        evidence_doc=INFRA_DEPLOY,
        evidence_heading=(_DEPLOY_ROOT, "앞단 라우팅"),
        note="API 컨테이너 8000 포트. 나머지는 웹 8080 — 같은 오리진이라 CORS 를 타지 않는다.",
    ),
    DemoQuestion(
        key="Q12",
        content_ko="백업은 어떤 볼륨들을 함께 받아야 하나요?",
        expected_grades=_MIXED,
        evidence_doc=INFRA_DEPLOY,
        evidence_heading=(_DEPLOY_ROOT, "데이터와 백업"),
        note="DB 볼륨과 스토리지 볼륨 둘 다. DB 만 받으면 원본 파일이 복구되지 않는다.",
    ),
    DemoQuestion(
        key="Q13",
        content_ko="액세스 토큰은 몇 분 동안 유효한가요?",
        expected_grades=(GRADE_RED,),
        evidence_doc=FRONTEND_GUIDE,
        evidence_heading=(_GUIDE_ROOT, "Authentication and Token Handling"),
        note=(
            "강제 🔴 conflict — 영어 프론트 가이드는 30분, 한국어 운영 규칙은 60분이라고 적혀 "
            "있다. 시제·대상·확정성이 같고 수치만 다르므로 진짜 모순이다. **교차언어 충돌**이라 "
            "'문서가 두 언어로 나뉘어 있으면 이런 어긋남이 생긴다'는 데모 대사가 그대로 선다."
        ),
    ),
    DemoQuestion(
        key="Q14",
        content_ko="모바일 앱은 언제 출시되나요?",
        expected_grades=(GRADE_RED,),
        evidence_doc=None,
        evidence_heading=None,
        note=(
            "강제 🔴 no_evidence — 코퍼스 다섯 문서 어디에도 모바일·네이티브 앱 언급이 없다. "
            "⛔ 문서를 고칠 때 모바일이라는 단어를 넣지 마라. 이 계열이 조용히 🟡 로 바뀐다."
        ),
    ),
)

BY_KEY: dict[str, DemoQuestion] = {question.key: question for question in CANONICAL}

# 라이브 시연이 그대로 던지는 질문 — 원문을 이력에 소진하지 않는다.
LIVE_ONLY_KEYS: frozenset[str] = frozenset({"Q13", "Q14"})

# 승인(`verified`) 주입 제외 — 공식 Q&A 가 생기면 당일 재사용 경로로 빠진다.
NO_APPROVAL_KEYS: frozenset[str] = frozenset({"Q2", "Q13", "Q14"})

# 🔴 카드 확정 제외 — 확정도 공식 Q&A 를 만든다.
NO_RED_RESOLVE_KEYS: frozenset[str] = NO_APPROVAL_KEYS


@dataclass(frozen=True)
class HistoryQuestion:
    """`--with-history` 가 던지는 1건.

    ⚠️ `demo_questions.HistoryQuestion` 을 재사용하지 않는다. 그쪽 `approvable` 프로퍼티는
    **GlobalMart 의** `NO_APPROVAL_KEYS` 를 읽는데 두 질문셋의 키가 `Q1`~ 로 겹친다 —
    재사용하면 제외 대상이 조용히 다른 세트 기준으로 판정된다. 승인 제외의 정본은
    프로필이 들고 있는 키 집합이다.
    """

    family: str
    content_ko: str
    verbatim: bool = False


def _family(key: str, *paraphrases: str) -> tuple[HistoryQuestion, ...]:
    """원문 + 패러프레이즈. 원문이 `LIVE_ONLY_KEYS` 면 변형만 남는다."""
    items: list[HistoryQuestion] = []
    if key not in LIVE_ONLY_KEYS:
        items.append(HistoryQuestion(family=key, content_ko=BY_KEY[key].content_ko, verbatim=True))
    items.extend(HistoryQuestion(family=key, content_ko=text) for text in paraphrases)
    return tuple(items)


# --------------------------------------------------------------------------------------
# 이력 질문셋
#
# ⚠️ **문안에 주제 앵커를 넣는다.** "몇 초 뒤에 다시 시도하나요" 처럼 주제어(SSE·스트림)가
#    빠지면 유사도가 떨어져 `similarity_floor` 에 걸리고 강제 🔴 이 된다. 키워드를 억지로
#    채우라는 뜻이 아니라, 사람이 실제로 묻는 방식에는 주제어가 들어간다는 뜻이다.
#
# ⚠️ **영어 근거 계열(Q1~Q6)에 변형을 몰아 두었다.** 검색 축과 언어가 같아 🟢 이 안정적으로
#    나오는 쪽이다. 🟢 표본이 30건에 못 미치면 지표 화면이 전 등급 "표본 부족"으로 떠서
#    발표 마지막 화면이 빈다.
# --------------------------------------------------------------------------------------
HISTORY: tuple[HistoryQuestion, ...] = (
    *_family(
        "Q1",
        "SSE 연결이 끊어졌을 때 재구독까지 대기 시간이 얼마인가요?",
        "실시간 스트림이 끊기면 자동으로 다시 연결되나요?",
        "SSE 재연결 대기 시간이 3초가 맞나요?",
        "알림 스트림은 세션당 몇 개를 여나요?",
        "탭을 두 개 열면 SSE 스트림도 두 개가 되나요?",
        "SSE 스트림 앞단에서 버퍼링을 하면 어떻게 되나요?",
    ),
    *_family(
        "Q2",
        "프론트의 API 베이스 URL 환경변수는 빌드 시각에 번들로 확정되나요?",
        "프론트 API 베이스 URL 을 바꾸려면 이미지를 다시 빌드해야 하나요?",
        "빌드된 번들 안에 API 경로가 들어 있나요?",
        "API 주소 환경변수를 고쳤는데 반영이 안 되면 프론트 이미지를 다시 빌드해야 하나요?",
        "API 주소 환경변수는 빌드 시각에 박히나요 런타임에 읽히나요?",
        "프론트 API 베이스 URL 을 바꾸면 이미지를 다시 빌드해야 하나요?",
        "VITE_API_BASE_URL 값이 같은 오리진 구성에서는 무엇인가요?",
        "API 베이스 URL 환경변수만 고치고 컨테이너를 재시작하면 번들에 반영되나요?",
        "프론트 번들에 API 주소가 어떻게 들어가나요?",
        "API 베이스 URL 이 상대 경로면 CORS 프리플라이트가 발생하나요?",
        "프론트의 API 베이스 URL 을 런타임 환경변수로 바꿀 수 있나요?",
        "Vite 빌드 타임 환경변수를 바꾸고 재빌드를 빼먹으면 번들의 API 주소가 그대로인가요?",
        "프론트가 API 요청을 보내는 기본 경로가 무엇인가요?",
    ),
    *_family(
        "Q3",
        "assets 정적 번들에는 1년 immutable 캐시를, index.html 에는 no-cache 를 주나요?",
        "index.html 은 왜 캐시하지 않나요?",
        "assets 번들 파일명에 콘텐츠 해시가 붙어서 1년 캐시가 안전한가요?",
        "없는 번들 파일을 요청하면 index.html 로 폴백하나요?",
    ),
    *_family(
        "Q4",
        "확인 카드를 처리하는 인박스 화면과 브리핑 화면의 주소가 어떻게 되나요?",
        "담당자가 쓰는 인박스·브리핑·설정 화면의 경로는 각각 무엇인가요?",
        "질문 목록·문서함·알림 목록 화면의 경로가 각각 무엇인가요?",
        "프로젝트 설정 화면과 지표 대시보드 화면의 경로를 알려주세요.",
        "질문 목록 화면과 질문 상세 이력 화면의 경로가 무엇인가요?",
        "문서함 화면과 확인 카드 인박스 화면의 경로를 알려주세요.",
        "지표 대시보드 화면과 브리핑 화면의 경로가 각각 무엇인가요?",
        "공식 Q&A 화면과 지표 대시보드 화면의 경로가 각각 어떻게 되나요?",
        "로그인 없이 접근할 수 있는 화면은 무엇인가요?",
        "브리핑 화면과 공식 Q&A 화면의 경로가 각각 무엇인가요?",
        "멤버와 역할 관리 화면, 프로젝트 설정 화면의 경로가 각각 무엇인가요?",
        "로그인 없이 볼 수 있는 화면은 무엇이고 없는 경로는 어디로 이동하나요?",
        "질문을 새로 하는 화면과 질문 목록 화면의 경로가 각각 무엇인가요?",
    ),
    *_family(
        "Q5",
        "확인 대기 상태의 답변은 어떻게 보이나요?",
        "답변에 붙는 뱃지에는 어떤 종류가 있나요?",
        "질문자는 답변이 확정된 것인지 어떻게 구분하나요?",
        "초록 등급 답변은 매칭률과 근거 인용을 함께 펼쳐 보여 주나요?",
        "빨간 등급 답변도 질문자 화면에 발행되나요?",
        "초록 등급 답변은 인용을 어떻게 보여 주나요?",
        "노란 등급 답변에는 담당자 확인 대기 뱃지가 붙나요?",
        "질문자가 누르는 크로스체크 버튼은 몇 개인가요?",
        "달랐음 버튼을 누르면 답변이 어디로 가나요?",
        "보류된 답변은 질문 상세 화면에 어떻게 표시되나요?",
        "답변 화면에 매칭률이 함께 표시되나요?",
        "크로스체크 버튼은 어떤 답변 아래에 나타나나요?",
    ),
    *_family(
        "Q6",
        "문서 인제스트 진행 상태를 화면에서 볼 수 있나요?",
        "문서 버전이 거치는 상태에는 어떤 것들이 있나요?",
        "인제스트가 실패한 문서 버전은 화면에 어떻게 표시되나요?",
        "문서 버전은 처리 중·준비됨·실패 중 어떤 상태를 거치고 준비된 것만 활성화되나요?",
    ),
    *_family(
        "Q7",
        "매칭률이 S 와 G 의 평균인가요?",
        "근거 충족도 G 의 분모는 생성 시점 원본 문장 수로 고정되나요?",
        "근거 없는 문장을 잘라내면 매칭률이 올라가나요?",
        "검색 점수 S 는 어떤 값을 다시 스케일한 것인가요?",
        "매칭률 계산에 top-1 청크의 유사도가 쓰이나요?",
        "S 와 G 중 하나만 높으면 매칭률은 어떻게 되나요?",
        "매칭률 리스케일 구간의 상한과 하한 설정 키가 무엇인가요?",
    ),
    *_family(
        "Q8",
        "근거끼리 서로 다른 값을 말하면 어떤 강제 보류 사유가 되나요?",
        "검색 top-1 유사도가 similarity_floor 아래면 어떤 강제 보류 사유가 되나요?",
        "일일 LLM 호출 상한을 넘기면 어떤 강제 보류 사유로 떨어지나요?",
        "강제 보류 네 가지는 방해금지 시간대에도 보류 등급을 유지하나요?",
        "모델 출력이 스키마를 못 맞추면 어떤 강제 보류 사유로 떨어지나요?",
        "강제 보류 조건 네 가지는 매칭률 점수와 무관하게 적용되나요?",
        "강제 보류 네 가지 중 방해금지 시간대에 등급이 낮아지는 것이 있나요?",
    ),
    *_family(
        "Q9",
        "재사용된 공식 Q&A 답변은 확정 당시 한국어 원문을 그대로 쓰나요?",
        "공식 Q&A 를 다시 쓰면 번역이 다시 도나요?",
        "확정 답변을 재사용할 때 원문이 바뀌나요?",
        "재사용 답변에도 확인 카드가 생기나요?",
        "공식 Q&A 재사용 판정선과 비슷한 답변 첨부선의 설정 키 이름이 무엇인가요?",
        "재사용된 답변도 확인 카드를 만드나요?",
        "재사용 답변은 어떤 상태로 시작하나요?",
        "재사용까지는 아니지만 비슷한 확정 답변은 어떻게 처리되나요?",
        "공식 Q&A 재사용 판정은 질문의 영어 번역문 임베딩으로 비교하나요?",
        "공식 Q&A 재사용이 성립하면 파이프라인을 더 돌리지 않고 확정 답을 그대로 돌려주나요?",
    ),
    *_family(
        "Q10",
        "브리핑 시각과 방해금지 판정은 담당자 계정의 타임존을 기준으로 하나요?",
        "방해금지 시간대에 도착한 확신 부족 답변은 브리핑으로 묶여 전달되나요?",
        "담당자를 교체하면 인박스의 카드가 사라지나요?",
        "알림 전송이 실패하면 확인 카드도 함께 사라지나요?",
        "방해금지를 꺼도 이전에 쌓인 카드가 남아 있나요?",
        "알림 전송이 실패해도 인박스의 확인 카드는 그대로 남나요?",
    ),
    *_family(
        "Q11",
        "프론트 nginx 에 API 프록시 블록을 두나요?",
        "터널 라우팅에서 API 가 아닌 경로는 어디로 가나요?",
    ),
    *_family(
        "Q12",
        "데이터베이스 볼륨만 백업하면 무엇이 복구되지 않나요?",
        "업로드 원본 파일은 스토리지 볼륨에, 청크와 임베딩은 데이터베이스 볼륨에 들어가나요?",
        "데모 데이터는 시드 스크립트가 넣나요, 배포 스크립트가 함께 넣나요?",
        "데이터베이스 볼륨만 백업하면 업로드 원문을 복구할 수 있나요?",
        "볼륨이 이미 있으면 데이터베이스 초기화가 다시 도나요?",
    ),
    # 🔴 계열 — 원문은 라이브용으로 남기고 변형만 던진다.
    *_family(
        "Q13",
        "액세스 토큰 유효기간이 30분인가요 60분인가요?",
        "액세스 토큰 수명이 문서마다 다르게 적혀 있나요?",
        "액세스 토큰은 발급 후 얼마나 지나면 만료되나요?",
        "리프레시 토큰과 액세스 토큰의 유효기간이 각각 어떻게 되나요?",
    ),
    *_family(
        "Q14",
        "모바일 앱에서도 확인 카드를 처리할 수 있나요?",
        "네이티브 앱 버전 계획이 있나요?",
        "오프라인 상태에서 질문을 저장했다가 나중에 보낼 수 있나요?",
        "모바일 푸시 알림을 지원하나요?",
    ),
)
