"""업로드 본문 크기 제한 (`03 §7` — 파일당 20MB).

⚠️ **서비스 레이어의 `_read_capped` 만으로는 상한이 방어가 아니라 표시다.**
`File(...)` 의존성은 라우터 본문이 실행되기 **전에** multipart 전체를 파싱하고,
Starlette 은 파일 파트에 크기 상한을 걸지 않는다 (`starlette/formparsers.py` — `max_part_size`
검사는 파일이 **아닌** 필드에만 적용된다). 파일 파트는 `SpooledTemporaryFile` 로 가고
1MB 를 넘는 순간 OS 임시 디렉터리의 실제 파일로 롤오버된다. 즉 10GB 를 올리면
10GB 가 전부 전송·디스크 기록된 **뒤에** 400 이 나간다 — 디스크 고갈이다.

그래서 상한은 **폼 파싱 이전**, 즉 미들웨어에서 한 번 더 건다:

1. `Content-Length` 선언값이 상한을 넘으면 본문을 읽지 않고 즉시 400.
2. 실제 수신 바이트도 센다 — `Content-Length` 위조와 `Transfer-Encoding: chunked` 대비.

`app/services/document_service.py:_read_capped` 는 그대로 두되 **2차 방어선**이다.
이쪽은 본문 전체(멀티파트 경계·다른 필드 포함) 기준이라 파일 하나의 정확한 크기 판정은
여전히 서비스 레이어가 한다.
"""

import json

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ValidationError, error_payload


class UploadSizeLimitMiddleware:
    """본문이 `max_body_bytes` 를 넘으면 400 `VALIDATION_ERROR` (`05 §1.4` 의 기존 코드)."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            # 본문을 한 바이트도 읽지 않고 끊는다.
            # ⚠️ 이 미들웨어는 ExceptionMiddleware **위**에 있어 AppError 핸들러가 닿지 않는다 —
            #    응답을 직접 만들어 보낸다(포맷은 `05 §1.4` 그대로).
            await self._reject(scope, receive, send)
            return

        received = 0
        exceeded = False
        replaced = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    # 본문 읽기를 여기서 끊는 것이 실제 방어다 — 이 예외가 폼 파싱을 중단시킨다.
                    raise ValidationError(_too_large_message(self.max_body_bytes))
            return message

        async def guarded_send(message: Message) -> None:
            # ⚠️ FastAPI 는 본문 파싱 중의 예외를 **넓게 잡아** `HTTPException(400, "There was an
            #    error parsing the body")` 로 바꾼다 (`fastapi/routing.py`). 그래서 위 예외는
            #    호출자에게 "요청 형식 오류"로 도착하고 진짜 사유가 사라진다.
            #    상한을 넘긴 것이 확실한 요청은 여기서 응답을 갈아끼워 사유를 되살린다.
            nonlocal replaced
            if not exceeded:
                await send(message)
                return
            if message["type"] == "http.response.start":
                await _send_too_large(send, self.max_body_bytes)
                replaced = True
            elif not replaced:
                await send(message)
            # replaced 이후의 본문 메시지는 버린다 — 응답은 이미 완결됐다.

        await self.app(scope, counting_receive, guarded_send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=400,
            content=error_payload("VALIDATION_ERROR", _too_large_message(self.max_body_bytes)),
        )
        await response(scope, receive, send)


async def _send_too_large(send: Send, max_body_bytes: int) -> None:
    body = json.dumps(
        error_payload("VALIDATION_ERROR", _too_large_message(max_body_bytes)),
        ensure_ascii=False,
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _declared_length(scope: Scope) -> int | None:
    raw = Headers(scope=scope).get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _too_large_message(max_body_bytes: int) -> str:
    return f"요청 본문이 너무 큽니다. 최대 {max_body_bytes // (1024 * 1024)}MB 입니다."
