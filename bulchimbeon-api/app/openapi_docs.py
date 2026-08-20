"""OpenAPI(Swagger) 스키마 보강 — 계약서(`05`)의 전역 규약을 라이브 문서로 옮긴다.

각 오퍼레이션의 설명은 **이미 라우터 독스트링이 채우고 있다**(FastAPI 기본 동작). 이 모듈이
채우는 것은 오퍼레이션 하나만 봐서는 알 수 없는 것들이다:

- 전역 규약 — 인증, 에러 봉투(`05 §1.4`), 페이지네이션(`05 §1.2`), 언어(`05 §1.5`)
- 태그별 도메인 설명 — 이 그룹을 **어떤 역할이** 쓰는가
- 실제 에러 응답 — FastAPI 기본값은 이 앱에서 **틀리다**(`_apply_common_responses` 참조)

⚠️ **계약을 바꾸지 않는다.** 여기서 하는 일은 이미 있는 동작을 서술하는 것뿐이다. 응답 필드나
   경로를 새로 만들지 않는다 (`CLAUDE.md` 룰 7 — 계약서에 없는 것을 만들지 않는다).
   그래서 이 모듈은 `create_app` 이 **다 조립한 뒤** 스키마만 후처리한다.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.config import (
    DEFAULT_SETTINGS,
    MAX_DOCUMENTS_PER_PROJECT,
    MAX_UPLOAD_BYTES,
    settings,
)
from app.core.errors import AppError

# 스키마 컴포넌트 이름. 계약서 §1.4 의 봉투에 붙이는 이름이며 응답 본문을 바꾸지 않는다.
ERROR_SCHEMA_NAME = "ErrorEnvelope"
_ERROR_REF = {"$ref": f"#/components/schemas/{ERROR_SCHEMA_NAME}"}

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})


API_SUMMARY = "시차가 큰 글로벌 팀을 위한 비동기 Q&A 협업 서비스"

API_DESCRIPTION = f"""
AI가 프로젝트 문서를 근거로 답변을 만들고, **담당자 확인을 거쳐** 지식으로 환류하는 백엔드다.

이 페이지는 **라이브 스키마**다. 필드의 모양은 여기가 정확하지만, "왜 그렇게 동작하는가"는
저장소의 설계 문서에 있다 — 충돌 시 항상 `docs/02-business-rules.md` 가 이긴다.

| 문서 | 내용 |
| --- | --- |
| `docs/05-api-contract.md` | **API 계약서** — 이 스키마의 원본 |
| `docs/02-business-rules.md` | 동작 규칙 (최우선 기준) |
| `docs/04-data-model.md` | 테이블·상태 전이 |
| `docs/06-ai-pipeline.md` | 답변 파이프라인 단계 |

---

## Base URL

**이 서버는 `/api` 아래에서만 응답한다.** 루트에 걸린 경로는 하나도 없다 — 프론트와 같은
리버스 프록시를 쓰기 위해서이며, 프록시는 `/api` 한 줄만 백엔드로 넘기면 된다.

| 경로 | 내용 |
| --- | --- |
| `/api/v1/...` | 업무 API 전부 |
| `/api/health` | DB ping 포함 헬스체크 (인증 불필요) |
| `/api/docs` · `/api/redoc` · `/api/openapi.json` | 이 문서 자신 |

`/api/health` 는 **DB가 죽어도 200** 이고 상태는 본문 `db` 필드로만 알린다. 5xx를 주면 배포
플랫폼이 기동 실패로 보고 마이그레이션 도중 재시작 루프에 빠진다.

---

## 인증

`POST /api/v1/auth/login` 이 돌려주는 **access token 을 `Authorization: Bearer <token>`** 헤더로
보낸다. 오른쪽 위 **Authorize** 버튼에 access token 을 넣으면 이 페이지에서 바로 호출할 수 있다.

| 토큰 | 수명 | 갱신 |
| --- | --- | --- |
| access | {settings.access_token_expire_minutes}분 | `POST /auth/refresh` |
| refresh | {settings.refresh_token_expire_days}일 | 재로그인 |

> 위 수명은 **이 인스턴스의 실제 설정값**이다(`ACCESS_TOKEN_EXPIRE_MINUTES` ·
> `REFRESH_TOKEN_EXPIRE_DAYS`). 환경마다 다를 수 있다.

401을 받으면 refresh 로 한 번 갱신하고, 그마저 실패하면 두 토큰을 버리고 로그인 화면으로 보낸다.

---

## 권한 — 서버가 강제한다

프로젝트마다 역할이 **담당자(answerer) 1명 + 질문자(asker) N명**이다. 역할 판정은 전부 서버
의존성에서 이뤄지며 **프론트를 신뢰하지 않는다**.

| 역할 | 할 수 있는 것 |
| --- | --- |
| 담당자 | 문서·연동·설정·멤버 관리, 확인 카드 큐 처리, 브리핑, 사용량·비용 조회 |
| 질문자 | 질문 접수, 자기 질문 조회, 답변 크로스체크("맞았다"/"달랐다") |

⚠️ **담당자는 자기 프로젝트에 질문할 수 없다**(403 `FORBIDDEN_ROLE`). 확정은 담당자의 권한이고
질문자가 할 수 있는 건 크로스체크뿐이다.

### 404 와 403 을 나누는 기준

**남의 프로젝트 리소스는 403이 아니라 404** 다. 403을 주면 "그 id 의 리소스가 존재한다"는 사실이
비멤버에게 새어 나간다. 같은 프로젝트 안에서 역할이 모자란 경우에만 403이다.

---

## 에러 응답

성공이 아닌 응답은 **항상** 이 봉투다. 형태가 코드마다 달라지지 않는다.

```json
{{"error": {{"code": "FORBIDDEN_ROLE", "message": "담당자만 수행할 수 있습니다."}}}}
```

⚠️ **`message` 는 개발자용 한국어 고정이다.** 사용자에게 그대로 보여주기 위한 값이 아니다 —
프론트는 `code` 로 분기하고 문안은 자기 로케일에서 만든다.

| HTTP | `code` | 뜻 |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | 요청 형식 오류 (**FastAPI 기본 422가 아니다**) |
| 400 | `UNSUPPORTED_FILE_TYPE` | 허용하지 않는 확장자 |
| 401 | `UNAUTHORIZED` | 토큰 없음·무효 |
| 401 | `TOKEN_EXPIRED` | 토큰 만료 → refresh |
| 403 | `FORBIDDEN_ROLE` | 역할이 모자람 |
| 403 | `NOT_MEMBER` | 프로젝트 멤버가 아님 |
| 404 | `NOT_FOUND` | 없거나, **볼 권한이 없음** |
| 409 | `ALREADY_RESOLVED` | 이미 처리된 카드 (기존 처리 결과를 함께 실어 준다) |
| 409 | `DUPLICATE_FEEDBACK` | 이미 피드백함 |
| 409 | `INVITE_ALREADY_JOINED` | 이미 참여한 프로젝트 |
| 409 | `FEEDBACK_NOT_ALLOWED` | 피드백할 수 없는 상태의 답변 |
| 409 | `PIPELINE_IN_PROGRESS` | 파이프라인 처리 중 |
| 409 | `INVALID_CARD_ACTION` | 이 카드에 허용되지 않는 액션 |
| 422 | `PIPELINE_FAILED` | 파이프라인 실패 |
| 500 | `INTERNAL_ERROR` | 내부 오류 (사유를 본문에 싣지 않는다) |

---

## 목록 응답

두 가지 봉투가 있고 **엔드포인트마다 고정**이다.

```json
{{"items": [], "total": 0, "limit": 20, "offset": 0}}   // 질문·카드·알림·공식 Q&A·교훈
{{"items": []}}                                          // 프로젝트·문서·멤버·연동
```

---

## 답변 등급

| 등급 | 뜻 | 질문자에게 |
| --- | --- | --- |
| 🟢 `green` | 즉답 | 발행 (단 `draft` 로 시작하고 카드도 만든다) |
| 🟡 `yellow` | 확인 대기 | 발행 + 담당자 확인 카드 |
| 🔴 `red` | 보류 | **발행하지 않는다.** DB에 초안으로 남아 카드에 표시된다 |

`matching_rate` 는 검색 점수 S와 근거 점수 G의 **`min(S, G)`** 다 — 평균이 아니다.
`held` · 처리 중 · 재사용 답변에서는 `null` 이다.

⚠️ `citations[].similarity` 는 **원시 코사인**이라 `matching_rate` 와 스케일이 다르다.
퍼센트로 표시하면 안 된다.

**자동 확정 경로는 어디에도 없다.** 🟢도 담당자 확인을 거친다.

---

## 실시간 (SSE)

1. `POST /api/v1/sse/ticket` 으로 **1회용 티켓**을 받는다 (TTL 60초).
2. `GET /api/v1/sse/stream?ticket=...` 로 연결한다.

`EventSource` 가 헤더를 못 실어서 쿼리로 인증해야 하는데, access token 을 URL에 실으면
프록시·히스토리·액세스 로그에 평문으로 남는다. 그래서 SSE 전용 티켓을 따로 둔다 —
**연결이 성립하는 순간 소진되므로 재연결마다 새로 발급**한다.

이벤트 9종: `answer.completed` `answer.updated` `card.created` `card.resolved` `briefing.ready`
`document.ingested` `notification.created` `notification.unread_count` `sync.completed`

`notification.unread_count` 만 **스트림 시작 시 1회** 내려간다. 서버는 14초마다 `ping` 을 보내며,
30초 넘게 없으면 죽은 연결로 보고 재연결하면 된다. **재생(replay) 계약은 없다** — 끊긴 동안의
이벤트는 유실되므로 재연결 시 리소스를 다시 GET 한다.

---

## 제한값

| 항목 | 값 | 조정 |
| --- | --- | --- |
| 업로드 파일 크기 | {MAX_UPLOAD_BYTES // (1024 * 1024)}MB | 상수 (env 아님) |
| 프로젝트당 활성 문서 | {MAX_DOCUMENTS_PER_PROJECT}개 | 상수 (env 아님) |
| 일일 LLM 호출 | {DEFAULT_SETTINGS["daily_llm_call_limit"]}회 | `PATCH /projects/{{id}}/settings` |

등급·유사도·만료 같은 임계값은 하드코딩이 아니라 **`projects.settings` 16개 키**에 있다.
`PATCH /projects/{{id}}/settings` 로 조정하며, **표에 없는 키는 400** 이다.
""".strip()


# --------------------------------------------------------------------------------------
# 태그 설명 — 라우터가 붙이는 tags 값과 1:1 이다. 여기 없는 태그는 설명 없이 그대로 나온다.
# 순서가 곧 Swagger UI 의 그룹 순서이므로 "로그인 → 프로젝트 → 문서 → 질문" 흐름대로 둔다.
# --------------------------------------------------------------------------------------
TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "회원가입·로그인·토큰 갱신. **이 그룹만 인증 없이 호출한다.**\n\n"
            "`GET /auth/me` 는 유저 정보와 함께 참여 중인 프로젝트 목록·안 읽은 알림 수를 "
            "한 번에 돌려준다 — 앱 부팅 시 첫 호출로 설계된 자리다."
        ),
    },
    {
        "name": "projects",
        "description": (
            "프로젝트 생성·참여·설정·멤버. **생성자가 곧 담당자**다.\n\n"
            "초대는 이메일 발송이 아니라 **초대 코드 공유**다. "
            "재발급하면 기존 코드는 무효가 된다.\n\n"
            "`PATCH /{id}/settings` 는 임계값 16개 키만 받는다 — 목록 밖 키는 400이다. "
            "`briefing_timezone` 은 허용 키가 **아니다**: 브리핑·방해금지 시각의 단일 원천은 "
            "현재 담당자의 `users.timezone` 이며 프로젝트 설정으로 덮어쓸 수 없다."
        ),
    },
    {
        "name": "documents",
        "description": (
            "근거 문서와 버전. **담당자 전용 쓰기 · 멤버 읽기.**\n\n"
            "업로드는 201을 즉시 돌려주고 인제스트(파싱·청킹·임베딩)는 비동기로 돈다. "
            "완료는 SSE `document.ingested` 로 알리며, "
            "**그 전까지 해당 버전은 검색 대상이 아니다**.\n\n"
            "재업로드는 덮어쓰기가 아니라 **새 버전**이다. 활성 버전을 전환하는 시점에 "
            "그 문서를 근거로 쓴 기존 답변의 **재검토 연쇄**가 돈다 — 응답의 "
            "`review_cascade_count` 가 몇 건이 대상인지 알려준다.\n\n"
            "삭제는 soft delete 다. 행도 청크도 지우지 않고 검색에서만 빠진다."
        ),
    },
    {
        "name": "integrations",
        "description": (
            "Notion·GitHub 문서 동기화. **담당자 전용.**\n\n"
            "등록 시 받은 토큰은 DB에 암호화 저장되고 **응답에서는 항상 마스킹**된다 — "
            "평문 토큰은 어떤 응답에도 실리지 않는다.\n\n"
            "⚠️ 남의 프로젝트 연동은 403이 아니라 **404** 다. 토큰이 붙어 있는 리소스라 "
            "존재 여부조차 흘리지 않는다."
        ),
    },
    {
        "name": "questions",
        "description": (
            "질문 접수·조회와 답변 크로스체크.\n\n"
            "`POST /projects/{id}/questions` 는 **질문자 전용**이고 202로 즉시 반환한다 — "
            "파이프라인은 백그라운드에서 돌고 완료는 SSE `answer.completed` 로 온다. "
            "그래서 202 응답의 `suggest_urgent` 는 접수 시점 값이라 **항상 false** 다.\n\n"
            "긴급도 변경은 **작성자만**, **처리 중(`processing`)일 때만** 가능하다. "
            "담당자라도 남의 질문의 긴급도를 바꿀 수 없다 — 긴급 여부를 정하는 주체는 질문자다. "
            "질문 **본문은 수정할 수 없다**.\n\n"
            "크로스체크(`POST /answers/{id}/feedback`)는 질문자만 하며, "
            '**"달랐다"에는 사유가 필수**다.'
        ),
    },
    {
        "name": "messages",
        "description": (
            "사람 간 양방향 대화 채널. **멤버면 역할 무관** 읽고 쓴다 — 질문과 달리 "
            "담당자도 발화한다.\n\n"
            "질문 파이프라인과 완전히 분리돼 있다: AI 답변도, 알림·브리핑도 붙지 않는다. "
            "새 메시지는 SSE `message.created` 로 프로젝트 멤버 전원에게 통지되고, "
            "프론트는 수신 시 목록을 재조회한다.\n\n"
            "목록은 `created_at` **오름차순**이다 — 채팅 화면이 위에서 아래로 읽힌다."
        ),
    },
    {
        "name": "review-cards",
        "description": (
            "확인 카드 큐 — 담당자의 인박스. **담당자 전용.**\n\n"
            "정렬(승인 추천 → 긴급 → 오래된 순)은 **서버가 한다.** "
            "클라이언트가 다시 정렬하지 않는다.\n\n"
            "액션은 카드의 `reason` 에 따라 허용 여부가 다르다. 예를 들어 `failed` 카드에는 "
            "승인할 원안이 없다. 허용되지 않는 액션은 409 `INVALID_CARD_ACTION` 이다.\n\n"
            "⚠️ `GET /review-cards/{id}` 는 **부수 효과가 있는 유일한 GET** 이다 — 최초 열람 시각을 "
            "기록한다. 목록에서 호버 프리페치나 백그라운드 선행 조회를 하면 열지도 않은 카드에 "
            "열람 시각이 찍혀 `card_handle_30s_rate` 지표가 통째로 무의미해진다. "
            "**프리페치 금지.**\n\n"
            "퇴근 모드를 꺼도, 담당자를 교체해도, 알림이 실패해도 **카드는 사라지지 않는다.**"
        ),
    },
    {
        "name": "official-qas",
        "description": (
            "확정된 지식. 담당자가 카드를 승인하면 여기로 편입되고, 이후 충분히 유사한 질문에 "
            "**재사용**된다.\n\n"
            "재사용 답변은 확정 당시의 **한국어 원문 그대로** 나간다 — 재번역하지 않는다. "
            "`query` 파라미터는 본문 키워드 부분일치이며, "
            "재사용 판정에 쓰는 벡터 검색과는 별개다.\n\n"
            "삭제는 물리 삭제가 아니라 `status` 를 `archived` 로 바꾸는 것이다."
        ),
    },
    {
        "name": "lessons",
        "description": (
            "답변 품질을 높이는 프로젝트 고유 원칙. **담당자 전용.**\n\n"
            "AI가 후보(`candidate`)를 만들고 담당자가 승인해야 `approved` 가 되어 실제 프롬프트에 "
            "들어간다. 상한을 넘지 않도록 `cleanup_suggestions` 로 정리 대상 id 를 함께 준다 "
            "(오래되고 쓰이지 않은 순).\n\n"
            "⚠️ 삭제된 교훈은 되살릴 수 없다 — 404 다. "
            "담당자가 버린 원칙이 승인으로 부활하면 안 된다."
        ),
    },
    {
        "name": "notifications",
        "description": (
            "인앱 알림. **프로젝트가 아니라 유저 스코프**라 경로에 `project_id` 가 없다 "
            "(대상 프로젝트는 `payload.project_id` 에 있다).\n\n"
            "안 읽은 개수는 `GET /notifications/unread-count` 로도, 스트림 시작 시 1회 오는 "
            "SSE `notification.unread_count` 로도 받는다."
        ),
    },
    {
        "name": "briefing",
        "description": (
            "담당자의 아침 브리핑. **담당자 전용.**\n\n"
            "언제 호출해도 된다 — 스케줄러가 발송하지 못했을 때 화면에서 수동으로 새로 고치는 "
            "경로다. 날짜 판정 기준은 **담당자의 `users.timezone`** 이다.\n\n"
            '🟢 답변 카드는 브리핑에서 제외되다가, "맞았다" 2건으로 승인 추천이 되면 '
            "`recommend_approve[]` 에 등장한다."
        ),
    },
    {
        "name": "metrics",
        "description": (
            "지표 대시보드와 학습 곡선. **멤버 전체가 본다** — 성과 화면이다. "
            "단 `GET /projects/{id}/usage`(LLM 사용량·비용)만 **담당자 전용**이다: "
            "비용은 팀 성과가 아니라 소유자 정보다.\n\n"
            '⚠️ **비율의 `value: null` 은 "표본 없음"이지 0%가 아니다.** 프론트는 「측정 전」을 '
            "표시해야 한다. 등급별 정확도는 표본 30건 미만이면 `sufficient: false` 이고 "
            "비율 대신 `message` 를 쓴다 — 데모 규모의 비율이 정확도로 읽히지 않게 하는 것이 "
            "이 지표의 존재 이유다.\n\n"
            "⚠️ `saved_wait_hours` 는 실측이 아니라 **추정**이다. 가정치와 근거 건수를 함께 "
            "노출해야 한다 — 숫자만 크게 띄우면 근거를 되묻게 된다.\n\n"
            "`reasoning_tokens` 는 `output_tokens` 에 **이미 포함**돼 있다. "
            "합계에 더하면 이중 계상이다."
        ),
    },
    {
        "name": "events",
        "description": (
            "이력 타임라인. 모든 상태 변화의 단일 원천이다.\n\n"
            "`?entity_type=question&entity_id=...` 로 **질문 하나의 전체 여정**이 나온다 — "
            "접수·등급 산출·상태 전이·카드 생성/처리·크로스체크·교훈·공식 Q&A 편입이 "
            "한 타임라인이다. "
            "최근 `limit` 건을 **오래된 순**으로 돌려준다."
        ),
    },
    {
        "name": "sse",
        "description": (
            "실시간 스트림. 티켓 발급 → 연결 2단계다.\n\n"
            "⚠️ 티켓은 **1회용**이라 재연결마다 새로 발급해야 한다. 같은 URL로 다시 붙으면 401이다 "
            "— `EventSource` 의 기본 자동 재연결을 쓸 수 없는 이유다.\n\n"
            "서버는 access token 만료 시각에 스트림을 끊는다. 프론트는 refresh 후 새 티켓으로 "
            "다시 연결한다."
        ),
    },
    {
        "name": "health",
        "description": (
            "DB ping 을 포함한 헬스체크. **DB가 죽어도 200** 이고 상태는 본문 `db` 필드로만 알린다 "
            "(배포 플랫폼의 재시작 루프를 피하기 위해서다). `/api/v1` 접두사 밖에 있다."
        ),
    },
]


# --------------------------------------------------------------------------------------
# 에러 응답 컴포넌트
# --------------------------------------------------------------------------------------
def _error_schema() -> dict[str, Any]:
    """`05 §1.4` 의 봉투. 스키마일 뿐이며 런타임 응답을 바꾸지 않는다."""
    return {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "프론트는 **이 값으로 분기한다.** 계약서 §1.4 표의 어휘로 닫혀 있다."
                        ),
                        "example": "FORBIDDEN_ROLE",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "개발자용 한국어 고정 문구. 사용자에게 그대로 노출하는 값이 아니다."
                        ),
                        "example": "담당자만 수행할 수 있습니다.",
                    },
                },
                # 409 ALREADY_RESOLVED 처럼 추가 필드를 싣는 코드가 있다 (`05 §1.4`).
                "additionalProperties": True,
            }
        },
    }


def _response(description: str, examples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": _ERROR_REF, "examples": examples}},
    }


def _example(summary: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    error.update(extra)
    return {"summary": summary, "value": {"error": error}}


# 경로에 관계없이 사실인 응답만 여기 둔다. 특정 엔드포인트에서만 나는 409 계열은
# 라우터 독스트링이 설명한다 — 전 오퍼레이션에 붙이면 "이 API 는 409 를 낼 수 있다" 는
# 거짓말이 58곳에 퍼진다.
def _common_responses() -> dict[str, dict[str, Any]]:
    return {
        "400": _response(
            "요청 형식 오류. **FastAPI 기본값인 422가 아니다** — 전역 핸들러가 400으로 바꾼다.",
            {
                "validation": _example(
                    "필드 검증 실패", "VALIDATION_ERROR", "요청 형식이 올바르지 않습니다."
                )
            },
        ),
        "500": _response(
            "서버 오류. **사유를 본문에 싣지 않는다** — 토큰·문서 본문이 섞여 나갈 수 있다.",
            {"internal": _example("내부 오류", "INTERNAL_ERROR", "서버 오류가 발생했습니다.")},
        ),
    }


def _auth_responses() -> dict[str, dict[str, Any]]:
    """인증이 걸린 오퍼레이션에만 붙인다."""
    return {
        "401": _response(
            "인증 실패. `TOKEN_EXPIRED` 면 `POST /auth/refresh` 로 갱신하고 재시도한다.",
            {
                "missing": _example("토큰 없음·무효", "UNAUTHORIZED", "인증이 필요합니다."),
                "expired": _example("만료", "TOKEN_EXPIRED", "토큰이 만료되었습니다."),
            },
        ),
        "403": _response(
            "권한 없음. **남의 프로젝트 리소스는 403이 아니라 404** 라는 점에 주의한다.",
            {
                "role": _example("역할 부족", "FORBIDDEN_ROLE", "담당자만 수행할 수 있습니다."),
                "member": _example("비멤버", "NOT_MEMBER", "프로젝트 멤버가 아닙니다."),
            },
        ),
        "404": _response(
            "대상이 없거나 **볼 권한이 없다.** 둘을 구분해 주지 않는 것이 의도다 — "
            "403을 주면 그 id 의 리소스가 존재한다는 사실이 새어 나간다.",
            {"not_found": _example("없음", "NOT_FOUND", "대상을 찾을 수 없습니다.")},
        ),
    }


# --------------------------------------------------------------------------------------
# 라우트별 에러 문서 — `responses=openapi_docs.error_responses("ALREADY_RESOLVED", ...)`
#
# ⚠️ 문구를 여기 다시 적지 않는다. `core.errors` 의 클래스에서 status·message 를 그대로
#    읽어 온다. 에러 정의와 문서가 따로 놀면 "문서에는 409 인데 실제로는 400" 같은 차이가
#    생기고, 그 차이는 프론트가 화면에서 발견한다. 없는 코드를 넘기면 **import 시점이 아니라
#    호출 시점에** KeyError 로 죽는데, 라우터 정의는 모듈 로드 중에 평가되므로 결국
#    기동 시점에 잡힌다 (`schemas/project.py` 의 키 대조와 같은 방식이다).
# --------------------------------------------------------------------------------------
def _error_classes() -> dict[str, type[AppError]]:
    found: dict[str, type[AppError]] = {}

    def walk(cls: type[AppError]) -> None:
        for sub in cls.__subclasses__():
            found[sub.code] = sub
            walk(sub)

    walk(AppError)
    return found


_ERROR_CLASSES = _error_classes()

# 코드마다 "언제 나는가" 한 줄. `message` 는 무슨 일이 났는지만 말하고 어떤 조건에서
# 나는지는 말하지 않아서, 그 한 줄을 여기 둔다.
_ERROR_WHEN: dict[str, str] = {
    "ALREADY_RESOLVED": "다른 기기·다른 탭에서 이미 처리한 카드. 기존 처리 결과를 함께 싣는다.",
    "INVALID_CARD_ACTION": (
        "카드의 `reason` 이 허용하지 않는 액션 (예: `failed` 카드에는 승인할 원안이 없다)."
    ),
    "DUPLICATE_FEEDBACK": "같은 답변에 이미 피드백을 남겼다. 한 답변당 한 사람 한 번이다.",
    "FEEDBACK_NOT_ALLOWED": "재검토 중이거나 더 이상 유효하지 않은 답변.",
    "INVITE_ALREADY_JOINED": "이미 그 프로젝트의 멤버다.",
    "PIPELINE_IN_PROGRESS": "처리 중이라 지금은 바꿀 수 없다. 응답에 현재 `status` 를 함께 싣는다.",
    "PIPELINE_FAILED": "인제스트·파이프라인이 실패했다.",
    "UNSUPPORTED_FILE_TYPE": "허용 확장자가 아니다 (MD·TXT·PDF·DOCX).",
    "VALIDATION_ERROR": "요청 형식 오류. 업로드 상한 초과도 이 코드다.",
    "TOKEN_EXPIRED": "토큰이 만료됐다.",
    "UNAUTHORIZED": "토큰이 없거나 무효다.",
    "NOT_FOUND": "없거나, 볼 권한이 없다.",
}


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """계약서 §1.4 의 에러 코드들을 라우트 `responses=` 문서로 만든다.

    같은 status 의 여러 코드는 하나의 응답 안에 `examples` 로 묶인다 — Swagger UI 에서
    드롭다운으로 골라 볼 수 있다.
    """
    grouped: dict[int, list[type[AppError]]] = {}
    for code in codes:
        cls = _ERROR_CLASSES[code]
        grouped.setdefault(cls.status_code, []).append(cls)

    result: dict[int | str, dict[str, Any]] = {}
    for status, classes in grouped.items():
        examples = {
            cls.code: _example(_ERROR_WHEN.get(cls.code, cls.code), cls.code, cls.message)
            for cls in classes
        }
        description = "\n".join(
            f"- `{cls.code}` — {_ERROR_WHEN.get(cls.code, cls.message)}" for cls in classes
        )
        result[status] = _response(description, examples)
    return result


def _is_autogenerated_validation_error(response: Any) -> bool:
    """FastAPI 가 자동으로 붙인 422 인가.

    ⚠️ 판정은 **`$ref` 로만** 한다. 응답 전체를 문자열로 만들어 `"HTTPValidationError" in ...`
    를 보면, 설명 문구에 그 단어가 우연히 들어간 라우터 선언까지 지워 버린다. 이 함수가
    지우는 쪽으로 틀리면 **실제로 나는 응답이 문서에서 사라지므로** 좁게 판정한다.
    """
    if not isinstance(response, dict):
        return False
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return isinstance(schema, dict) and schema.get("$ref", "").endswith("/HTTPValidationError")


def _apply_common_responses(schema: dict[str, Any]) -> None:
    """모든 오퍼레이션에 공통 에러 응답을 채운다.

    ⚠️ **FastAPI 가 자동으로 넣는 422 를 지운다.** 이 앱은 `RequestValidationError` 를 전역
    핸들러에서 **400 `VALIDATION_ERROR`** 로 바꾸므로(`main._register_exception_handlers`),
    기본 422 문서는 실제 동작과 다르다. 게다가 본문 모양도 다르다 — FastAPI 의
    `HTTPValidationError` 는 `detail[]` 이지만 이 앱은 `{"error": {...}}` 봉투다.
    남겨 두면 프론트가 있지도 않은 `detail` 을 파싱하려 든다.

    ⚠️ 422 자체를 쓰지 않는 것은 아니다 — `PIPELINE_FAILED` 가 422 다. 그래서 지우는 대신
    라우터가 명시한 422 는 건드리지 않는다(`setdefault` 가 아니라 자동 생성분만 pop 한다).
    """
    common = _common_responses()
    auth = _auth_responses()

    for operations in schema.get("paths", {}).values():
        for method, operation in operations.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue

            responses = operation.setdefault("responses", {})

            if _is_autogenerated_validation_error(responses.get("422")):
                responses.pop("422")

            for status, payload in common.items():
                responses.setdefault(status, payload)

            # 인증이 걸린 오퍼레이션에만 401/403/404 를 붙인다. 로그인·회원가입·헬스체크에
            # 401 을 달면 "토큰이 필요하다" 는 반대 뜻이 된다.
            if operation.get("security"):
                for status, payload in auth.items():
                    responses.setdefault(status, payload)


def _describe_security_scheme(schema: dict[str, Any]) -> None:
    """Authorize 버튼에 "무엇을 넣어야 하는지" 를 적어 준다."""
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    for scheme in schemes.values():
        if isinstance(scheme, dict) and scheme.get("scheme") == "bearer":
            scheme["description"] = (
                "`POST /api/v1/auth/login` 의 `access_token` 을 넣는다 "
                "(`Bearer ` 접두사는 Swagger UI 가 붙인다).\n\n"
                "만료되면 401 `TOKEN_EXPIRED` 가 나오며, `POST /api/v1/auth/refresh` 로 "
                "갱신한 새 토큰을 다시 넣으면 된다."
            )


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """`app.openapi` 에 꽂을 커스텀 생성기. 결과는 `app.openapi_schema` 에 캐시된다."""
    if app.openapi_schema:
        return app.openapi_schema

    # ⚠️ FastAPI 의 기본 `openapi()` 가 넘기는 인자를 **전부** 그대로 넘긴다.
    #    일부만 넘기면 지금은 같은 결과가 나오지만, 나중에 `FastAPI(servers=..., contact=...)`
    #    를 추가한 사람은 그 값이 문서에서 조용히 사라지는 것을 보게 된다 — 이 함수가
    #    기본 동작을 **대체**하기 때문이다. 여기서는 값을 만들지 않고 앱에서 읽기만 한다.
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        terms_of_service=app.terms_of_service,
        contact=app.contact,
        license_info=app.license_info,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )

    schema.setdefault("components", {}).setdefault("schemas", {})[ERROR_SCHEMA_NAME] = (
        _error_schema()
    )
    _describe_security_scheme(schema)
    _apply_common_responses(schema)

    # 계약서를 라이브 문서 안에서 가리킨다 — 규칙의 최종 근거는 언제나 저장소의 문서다.
    schema["externalDocs"] = {
        "description": (
            "API 계약서 (docs/05-api-contract.md) · 동작 규칙 (docs/02-business-rules.md)"
        ),
        "url": "https://github.com/LetsMakeTrouble/bulchimbeon-backend/blob/main/bulchimbeon-api/docs/05-api-contract.md",
    }

    app.openapi_schema = schema
    return schema
