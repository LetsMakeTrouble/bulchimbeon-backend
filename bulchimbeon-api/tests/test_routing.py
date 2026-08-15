"""경로 접두사 규약 — **이 서버는 `/api` 아래에서만 응답한다.**

프론트와 같은 리버스 프록시를 쓰기 위한 전제다. 프록시는 `/api` 한 줄만 백엔드로 넘기고
나머지는 전부 프론트 정적 자산으로 보낸다. 루트에 걸린 경로가 하나라도 생기면 그 요청은
**프론트로 가서 404 나 SPA 인덱스가 되어 돌아온다** — 서버 로그에는 아무것도 남지 않아
원인을 찾기 어렵다.

⚠️ 이 규약은 라우터를 추가할 때보다 **프레임워크 기본값을 켤 때** 더 잘 깨진다.
`docs_url` 을 옮겼는데도 Swagger 의 `oauth2-redirect` 가 루트에 남아 있던 것이 실제 사례다.
그래서 개별 경로가 아니라 **"루트에 아무것도 없다"** 를 통째로 검사한다.
"""

from fastapi import FastAPI

from app.main import API_PREFIX, API_V1_PREFIX, create_app

# 문서·헬스체크만 `/api` 직하다. 나머지는 전부 `/api/v1` 아래에 있어야 한다.
_PREFIX_ROOT_ALLOWED = {
    f"{API_PREFIX}/health",
    f"{API_PREFIX}/docs",
    f"{API_PREFIX}/docs/oauth2-redirect",
    f"{API_PREFIX}/redoc",
    f"{API_PREFIX}/openapi.json",
}


def _mounted_paths(app: FastAPI) -> set[str]:
    """실제로 응답하는 모든 경로.

    두 곳을 합친다. **어느 한쪽만 보면 빠지는 것이 생긴다**:

    - `openapi()["paths"]` — 업무 엔드포인트 전부. 다만 `include_in_schema=False` 인
      프레임워크 경로(`/api/docs`, `oauth2-redirect`)는 여기 없다.
    - `app.routes` 의 `.path` — 그 프레임워크 경로들. 다만 `include_router` 로 붙은
      하위 라우터는 `.path` 가 없는 컨테이너 객체라 여기서는 보이지 않는다.

    ⚠️ 컨테이너 객체 내부를 파고들어 접두사를 조립할 수도 있지만, 그 구조는 FastAPI 의
    **비공개 구현**이라 버전이 오르면 조용히 빈 집합이 되고 이 테스트는 아무것도 검사하지
    않으면서 통과한다. 공개 API 두 개를 합치는 쪽이 오래 간다.
    """
    schema_paths = set(app.openapi()["paths"])
    route_paths = {route.path for route in app.routes if hasattr(route, "path")}
    return schema_paths | route_paths


def test_every_route_lives_under_the_api_prefix() -> None:
    paths = _mounted_paths(create_app())
    outside = sorted(p for p in paths if not p.startswith(f"{API_PREFIX}/"))
    assert not outside, f"`{API_PREFIX}` 밖에 남은 경로가 있다: {outside}"


def test_docs_and_health_are_under_the_prefix() -> None:
    """문서·헬스체크도 예외가 아니다 — `05 §14` 의 경로가 접두사 안으로 들어왔다."""
    paths = _mounted_paths(create_app())
    for expected in _PREFIX_ROOT_ALLOWED:
        assert expected in paths, f"{expected} 이 없다"


def test_business_api_stays_on_v1() -> None:
    """`/api` 로 옮긴다고 업무 API 의 `/api/v1` 이 흔들리면 안 된다 (`05 §1.1`)."""
    paths = _mounted_paths(create_app())
    versionless = sorted(
        p for p in paths if not p.startswith(f"{API_V1_PREFIX}/") and p not in _PREFIX_ROOT_ALLOWED
    )
    assert not versionless, f"버전 밖에 놓인 업무 경로가 있다: {versionless}"


def test_business_routes_are_actually_collected() -> None:
    """위 검사들이 **빈 집합을 통과시키지 않는지** 확인한다.

    `_mounted_paths` 가 라우트를 하나도 못 모으면 세 테스트가 전부 통과한다 — 규약을 지켜서가
    아니라 검사할 것이 없어서다. 실제로 `app.routes` 만 보던 첫 판이 업무 경로 0개를 세고도
    통과했다. 그 실패 모드를 여기서 막는다.
    """
    business = [p for p in _mounted_paths(create_app()) if p.startswith(f"{API_V1_PREFIX}/")]
    assert len(business) > 40, f"업무 경로가 너무 적게 잡혔다({len(business)}개) — 수집이 깨졌다"
