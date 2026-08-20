"""앱 팩토리 — CORS, 전역 예외 핸들러, /api/health.

라우터는 마일스톤마다 추가된다 (`03 §3`). 아직 없는 모듈은 미리 만들지 않는다.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, openapi_docs
from app.config import MAX_REQUEST_BODY_BYTES, settings
from app.core.errors import AppError, error_payload
from app.core.scheduler import create_scheduler, run_startup_jobs
from app.core.upload_limit import UploadSizeLimitMiddleware
from app.database import get_db
from app.routers import (
    auth,
    briefing,
    documents,
    integrations,
    lessons,
    messages,
    metrics,
    notifications,
    official_qas,
    projects,
    questions,
    review_cards,
    sse,
)

# ⚠️ import 만으로 SSE 아웃박스의 `after_commit` 훅이 등록된다 (`services/sse_manager.py`).
#    라우터를 통해 전이적으로 들어오지만, 발행이 이 import 에 걸려 있다는 사실을 여기 남긴다.
from app.services import sse_manager  # noqa: F401

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# 경로 접두사.
#
# **이 서버로 들어오는 모든 것은 `/api` 아래에 있다.** 프론트와 같은 리버스 프록시를 쓰기
# 위해서다 — 프록시는 `/api` 한 줄만 백엔드로 넘기고 나머지는 전부 프론트 정적 자산으로
# 보낸다. 루트에 걸린 경로가 하나라도 있으면 그 한 줄로는 부족해져 프록시 규칙이 늘어난다.
#
# ⚠️ 그래서 헬스체크와 문서도 **접두사 밖에 두지 않는다**:
#     `/health` → `/api/health` · `/docs` → `/api/docs` · `/openapi.json` → `/api/openapi.json`
#   `05 §14` 는 `GET /health` 로 적혀 있으나 그것은 배포 형상이 정해지기 전의 값이다.
#   응답 본문은 그대로이므로 계약의 **모양**은 바뀌지 않는다 — 경로만 접두사 안으로 들어온다.
#
# ⛔ 경로를 바꾸면 **compose 의 healthcheck 도 함께 바꿔야 한다.** 안 그러면 컨테이너가
#    영원히 unhealthy 로 남고, `depends_on: service_healthy` 에 걸린 서비스가 기동하지 않는다.
# --------------------------------------------------------------------------------------
API_PREFIX = "/api"
# `05 §1.1` — 업무 API 의 Base URL 은 `{HOST}/api/v1` 이다.
API_V1_PREFIX = f"{API_PREFIX}/v1"

DOCS_URL = f"{API_PREFIX}/docs"
REDOC_URL = f"{API_PREFIX}/redoc"
OPENAPI_URL = f"{API_PREFIX}/openapi.json"
HEALTH_PATH = f"{API_PREFIX}/health"

# ⚠️ Swagger UI 가 자동으로 다는 경로다. 기본값이 **`/docs/oauth2-redirect` 로 루트에 박혀**
#    `docs_url` 만 옮기면 이 하나가 `/api` 밖에 남는다. 이 앱은 Bearer 인증이라 실제로 쓰이지
#    않지만, "루트에는 아무것도 없다" 가 프록시 규칙의 전제이므로 함께 옮긴다.
SWAGGER_OAUTH2_REDIRECT_URL = f"{DOCS_URL}/oauth2-redirect"

# FastAPI 내부에서 raw HTTPException 이 올라올 때의 매핑.
# 도메인 로직은 AppError 를 쓰므로 여기 걸리는 건 라우팅 404·메서드 405 같은 프레임워크 예외다.
# `05 §1.5` — error.message 는 **개발자용 한국어 고정**이다. starlette 의 영문 detail 을
# 그대로 흘리면 계약을 벗어나므로 한국어 문안으로 갈아끼운다(원문은 로그에만 남긴다).
_HTTP_STATUS_TO_ERROR = {
    400: ("VALIDATION_ERROR", "요청 형식이 올바르지 않습니다."),
    401: ("UNAUTHORIZED", "인증이 필요합니다."),
    403: ("FORBIDDEN_ROLE", "권한이 없습니다."),
    404: ("NOT_FOUND", "대상을 찾을 수 없습니다."),
    422: ("PIPELINE_FAILED", "요청을 처리할 수 없습니다."),
}
_FALLBACK_SERVER_ERROR = ("INTERNAL_ERROR", "서버 오류가 발생했습니다.")
# 405 처럼 계약서에 코드가 없는 상태값. 임의로 코드를 신설하지 않고 400 계열로 떨어뜨린다.
_FALLBACK_CLIENT_ERROR = ("VALIDATION_ERROR", "요청을 처리할 수 없습니다.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """APScheduler 기동·정지 (`03 §3` — 만료 스위퍼 · 좀비 회수).

    ⚠️ **`--workers 1` 전제다** (룰 9). 워커마다 이 lifespan 이 돌아 스케줄러가 워커 수만큼
    발화한다. 자세한 근거와 확장 시점의 정답은 `app/core/scheduler.py` 독스트링.

    테스트는 ASGI transport 로 붙어 lifespan 을 돌리지 않으므로(httpx `ASGITransport` 기본)
    잡이 테스트 중에 끼어들지 않는다. 잡 본체는 `sweeper_service` 에 있고 시각을 주입받으므로
    직접 호출로 검증한다.
    """
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("scheduler 기동: 잡 %s개", len(scheduler.get_jobs()))

    # ⚠️ 기동 직후 1회 실행. `interval` 트리거의 첫 발화가 "기동 + 주기"라, 이게 없으면
    #    재배포할 때마다 최대 60분(브리핑)·10분(만료) 동안 아무 잡도 안 돈다.
    #    태스크로 띄워 기동을 막지 않는다 — 헬스체크가 먼저 통과해야 배포가 성립한다.
    startup_task = asyncio.create_task(run_startup_jobs())
    try:
        yield
    finally:
        startup_task.cancel()
        scheduler.shutdown(wait=False)
        logger.info("scheduler 정지")


def create_app() -> FastAPI:
    app = FastAPI(
        title="불침번 API",
        version=__version__,
        summary=openapi_docs.API_SUMMARY,
        description=openapi_docs.API_DESCRIPTION,
        openapi_tags=openapi_docs.TAGS_METADATA,
        lifespan=lifespan,
        # 문서도 `/api` 안이다 — 위 접두사 주석 참조.
        docs_url=DOCS_URL,
        redoc_url=REDOC_URL,
        openapi_url=OPENAPI_URL,
        swagger_ui_oauth2_redirect_url=SWAGGER_OAUTH2_REDIRECT_URL,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ⚠️ 업로드 상한은 **폼 파싱 이전**에 걸어야 방어가 된다 (`03 §7`).
    # 라우터 안에서 재는 것만으로는 Starlette 이 이미 본문 전체를 임시 파일로 떨군 뒤다.
    app.add_middleware(UploadSizeLimitMiddleware, max_body_bytes=MAX_REQUEST_BODY_BYTES)

    _register_exception_handlers(app)
    _register_health(app)

    app.include_router(auth.router, prefix=API_V1_PREFIX)
    app.include_router(projects.router, prefix=API_V1_PREFIX)
    app.include_router(documents.router, prefix=API_V1_PREFIX)
    app.include_router(integrations.router, prefix=API_V1_PREFIX)
    app.include_router(questions.router, prefix=API_V1_PREFIX)
    app.include_router(messages.router, prefix=API_V1_PREFIX)
    app.include_router(review_cards.router, prefix=API_V1_PREFIX)
    app.include_router(official_qas.router, prefix=API_V1_PREFIX)
    app.include_router(lessons.router, prefix=API_V1_PREFIX)
    app.include_router(notifications.router, prefix=API_V1_PREFIX)
    app.include_router(briefing.router, prefix=API_V1_PREFIX)
    app.include_router(metrics.router, prefix=API_V1_PREFIX)
    app.include_router(sse.router, prefix=API_V1_PREFIX)

    # ⚠️ 라우터를 **다 붙인 뒤**에 꽂는다. 스키마 생성기는 `app.routes` 를 훑으므로
    #    먼저 꽂아도 동작은 하지만, 이 순서를 지켜야 "문서는 조립 결과를 서술할 뿐"이라는
    #    관계가 코드에서도 읽힌다. 생성 결과는 `app.openapi_schema` 에 캐시된다.
    app.openapi = lambda: openapi_docs.build_openapi(app)  # type: ignore[method-assign]
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 계약서는 요청 형식 오류를 400 VALIDATION_ERROR 로 정의한다 (`05 §1.4`).
        # FastAPI 기본값(422)을 그대로 두면 422 PIPELINE_FAILED 와 의미가 겹친다.
        #
        # ⚠️ `exc.errors()` 를 응답에도 로그에도 그대로 싣지 않는다. 항목마다 제출된 원본 값이
        #    `input` 에 들어 있어 `POST /auth/signup` 에서 **평문 비밀번호가 그대로 새어 나간다**
        #    (`03 §7` 로그 유출 금지). `ctx` 에는 ValueError 객체가 들어와 직렬화도 실패한다.
        #    게다가 `detail` 은 `05 §1.4` 의 error 객체에 없는 필드다 — 계약 위반이기도 하다.
        safe_errors = [
            {"type": error.get("type"), "loc": error.get("loc"), "msg": error.get("msg")}
            for error in exc.errors()
        ]
        logger.warning(
            "request validation failed on %s %s: %s", request.method, request.url.path, safe_errors
        )
        return JSONResponse(
            status_code=400,
            content=error_payload("VALIDATION_ERROR", "요청 형식이 올바르지 않습니다."),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        fallback = _FALLBACK_SERVER_ERROR if exc.status_code >= 500 else _FALLBACK_CLIENT_ERROR
        code, message = _HTTP_STATUS_TO_ERROR.get(exc.status_code, fallback)
        logger.info(
            "http exception on %s %s -> %s (%s)",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return JSONResponse(status_code=exc.status_code, content=error_payload(code, message))

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # 내부 사정을 응답에 싣지 않는다. 토큰·문서 본문이 섞여 나갈 수 있다 (`03 §7`).
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_payload("INTERNAL_ERROR", "서버 오류가 발생했습니다."),
        )


def _register_health(app: FastAPI) -> None:
    @app.get(HEALTH_PATH, tags=["health"])
    async def health(db: AsyncSession = Depends(get_db)) -> JSONResponse:
        """DB ping 포함 헬스체크 (`03 §5.1`).

        ⚠️ **DB 가 죽어도 200 을 반환한다.** 상태는 본문의 `db` 필드로만 알린다.
        실패 시 5xx 를 주면 배포 플랫폼(Railway/Render)의 헬스체크가 이를 기동 실패로 보고
        `alembic upgrade head` 가 끝나기 전 첫 부팅을 죽여 재시작 루프가 된다.
        `00-kickoff:44` 과 `03 §5.1` 은 정상 응답만 규정하므로 실패 코드를 신설하지 않는다.

        세션 생성은 커넥션을 열지 않으므로(첫 execute 에서 연다) 의존성 단계는 항상 통과하고,
        실제 실패는 여기서 잡힌다.
        """
        db_status = "ok"
        try:
            await db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("health check: database ping failed")
            db_status = "error"

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok" if db_status == "ok" else "degraded",
                "db": db_status,
                "version": __version__,
            },
        )


app = create_app()
