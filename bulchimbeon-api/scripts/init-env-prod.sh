#!/usr/bin/env sh
# `.env.prod` 의 빈 비밀값을 생성해 채운다 (`.env.prod.example` → `.env.prod`).
#
#   sh scripts/init-env-prod.sh            # .env.prod 를 만들거나 빈 칸만 채운다
#   sh scripts/init-env-prod.sh .env.stage # 다른 파일을 대상으로
#
# ⛔ **이미 값이 있는 키는 절대 덮어쓰지 않는다.** 재실행이 안전해야 하기 때문이기도 하지만,
#    진짜 이유는 `INTEGRATION_ENCRYPTION_KEY` 다 — 이 키가 바뀌면 DB 에 암호화돼 있는
#    노션·깃헙 토큰을 **전부 복호화할 수 없게 되고** 모든 연동을 재등록해야 한다
#    (`09 §3`). "그냥 다시 돌렸는데 연동이 죽었다" 는 사고를 문법으로 막는다.
#    로테이션이 필요하면 해당 줄을 손으로 비우고 다시 돌려라 — 그 정도 마찰이 있어야 한다.
#
# ⚠️ `OPENAI_API_KEY` 는 생성할 수 없다. 비어 있으면 경고만 하고 끝에서 실패시킨다.

# --------------------------------------------------------------------------------------
# ⛔ `source` / `.` 로 실행하지 마라. 감지되면 아무것도 하지 않고 되돌아간다.
#
# `source ./init-env-prod.sh` 는 두 가지를 **동시에** 깨뜨린다 (실제로 겪었다):
#   1. `$0` 이 스크립트 경로가 아니라 셸 이름(`bash`)이 된다 → 경로 추론이 한 단계
#      어긋나 엉뚱한 디렉터리에서 템플릿을 찾는다.
#   2. `set -e` 와 `exit` 가 **호출한 셸에 그대로 적용된다** → 실패하는 순간 로그인 셸이
#      같이 죽어 SSH 세션이 끊긴다. 에러를 읽을 새도 없이 로그아웃된다.
#
# `$BASH_SOURCE` 는 첨자 없이 써도 bash 에서 `${BASH_SOURCE[0]}` 와 같고, dash/sh 에서는
# 그냥 비어 있는 변수라 문법 오류가 나지 않는다 — 그래서 이식성 있게 쓸 수 있다.
#
# ⚠️ 이 검사는 반드시 `set -eu` **앞**에 있어야 한다. 뒤에 두면 되돌아가기 전에 이미
#    호출한 셸의 옵션을 바꿔 놓아서, 그 뒤로 명령 하나만 실패해도 셸이 죽는다.
# --------------------------------------------------------------------------------------
_sourced=0
[ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE:-}" != "${0:-}" ] && _sourced=1
case "${ZSH_EVAL_CONTEXT:-}" in *:file:*) _sourced=1 ;; esac
if [ "$_sourced" = 1 ]; then
  echo "⛔ source 로 실행하지 마라 — 실패하면 로그인 셸까지 함께 죽는다." >&2
  echo "   이렇게 실행해라:  sh init-env-prod.sh" >&2
  return 1
fi

set -eu

# --------------------------------------------------------------------------------------
# 리포 루트 찾기.
#
# ⚠️ `dirname "$0"/..` 같은 **고정 상대 경로를 쓰지 않는다.** 스크립트를 `scripts/` 밖으로
#    옮기거나 리포 루트에 바로 놓는 순간 조용히 한 단계 위를 가리키고, 증상은
#    "템플릿이 없다: /root/bulchimbeon-backend/.env.prod.example" 로만 나타난다.
#    기준점을 "`.env.prod.example` 이 있는 가장 가까운 상위 디렉터리" 하나로 통일하면
#    스크립트가 어디에 놓여 있든, 어느 디렉터리에서 실행하든 같은 곳을 찾는다.
# --------------------------------------------------------------------------------------
find_root() {
  d=${1:-}
  while [ -n "$d" ] && [ "$d" != "/" ] && [ "$d" != "." ]; do
    [ -f "$d/.env.prod.example" ] && { printf '%s\n' "$d"; return 0; }
    d=$(dirname "$d")
  done
  return 1
}

script_dir=$(CDPATH= cd -- "$(dirname -- "${0:-.}")" 2>/dev/null && pwd) || script_dir=$PWD
ROOT=$(find_root "$script_dir") || ROOT=$(find_root "$PWD") || {
  echo "⛔ .env.prod.example 을 찾지 못했다." >&2
  echo "   리포 안(bulchimbeon-api/)에서 실행해라. 현재 위치: $PWD" >&2
  exit 1
}

TARGET_ARG="${1:-.env.prod}"
# ⚠️ Git Bash 에서는 `C:/...` 도 절대 경로다. `/*` 만 보면 저걸 상대 경로로 오인해
#    `<repo>/C:/...` 를 만들고 cp 가 실패한다 (개발자는 Windows, 배포는 Linux 다).
case "$TARGET_ARG" in
  /* | [A-Za-z]:[/\\]*) TARGET="$TARGET_ARG" ;;
  *)                    TARGET="$ROOT/$TARGET_ARG" ;;
esac
TEMPLATE="$ROOT/.env.prod.example"

# compose 가 `${VAR:?}` 로 요구하는 8개. 하나라도 비면 컨테이너가 기동 **전에** 죽는다.
# 그래서 이 스크립트의 마지막 일은 "compose 가 뜰 수 있는가" 를 미리 판정하는 것이다.
REQUIRED='POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB SECRET_KEY
INTEGRATION_ENCRYPTION_KEY OPENAI_API_KEY CORS_ORIGINS DEMO_PASSWORD'

# 이 중 생성 가능한 것만 자동으로 채운다.
GENERATED='POSTGRES_PASSWORD SECRET_KEY INTEGRATION_ENCRYPTION_KEY DEMO_PASSWORD'

# --------------------------------------------------------------------------------------
# 값 생성
#
# ⚠️ **`POSTGRES_PASSWORD` 만 hex 다 — 템플릿의 `-base64 24` 를 쓰지 않는다.**
#    이 값은 compose 가 URL 로 조립한다:
#        postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
#    base64 알파벳에는 `/` `+` `=` 가 있고, 그중 `/` 가 나오면 URL 의 authority 가 거기서
#    끊긴다. 비밀번호는 24바이트 중 한 글자만 `/` 여도 걸리므로 확률이 낮지도 않다
#    (base64 문자당 1/64, 32글자면 ~39%). hex 는 [0-9a-f] 뿐이라 URL·compose 양쪽에서
#    이스케이프가 필요 없다. 24바이트 = 192비트로 강도도 그대로다.
#
# 나머지 셋은 URL 에 들어가지 않으므로 base64 로 둔다. base64/hex 어느 쪽도
# `$` `#` `\` 를 만들지 않아 compose 의 env 파일 파싱과 아래 awk `-v` 에 안전하다.
# --------------------------------------------------------------------------------------
generate() {
  case "$1" in
    POSTGRES_PASSWORD)          openssl rand -hex 24 ;;
    SECRET_KEY)                 openssl rand -hex 32 ;;
    INTEGRATION_ENCRYPTION_KEY) openssl rand -base64 32 | tr -d '\n' ;;
    DEMO_PASSWORD)              openssl rand -base64 18 | tr -d '\n' ;;
    *) echo "generate: 모르는 키 $1" >&2; return 1 ;;
  esac
}

# `KEY=...` 줄의 값. 없거나 주석(`#KEY=`)이면 빈 문자열이다.
# 값에 `=` 가 들어갈 수 있어 첫 `=` 까지만 잘라낸다 (base64 의 패딩이 그렇다).
read_value() {
  awk -v k="$1" '
    index($0, k "=") == 1 { sub(/^[^=]*=/, ""); sub(/[[:space:]]+$/, ""); print; exit }
  ' "$TARGET"
}

# 같은 키의 첫 줄을 갈아끼운다. 줄이 없으면 파일 끝에 덧붙인다 —
# 누가 줄을 지운 채 돌려도 compose 가 뜨는 상태로 끝나야 한다.
write_value() {
  tmp="$TARGET.tmp.$$"
  awk -v k="$1" -v v="$2" '
    !done && index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$TARGET" > "$tmp"
  mv "$tmp" "$TARGET"
}

# --------------------------------------------------------------------------------------
# 템플릿 존재는 find_root 가 이미 보장한다 (그게 루트 판정 기준이다).
command -v openssl >/dev/null 2>&1 || { echo "⛔ openssl 이 없다." >&2; exit 1; }
echo "· 리포 루트: $ROOT"

if [ -f "$TARGET" ]; then
  echo "· 기존 파일을 쓴다: $TARGET"
  cp -p "$TARGET" "$TARGET.bak"
  echo "  백업: $TARGET.bak"
else
  cp "$TEMPLATE" "$TARGET"
  echo "· 템플릿에서 만들었다: $TARGET"
fi

# 비밀값 파일이다. umask 에 기대지 않고 명시적으로 좁힌다.
chmod 600 "$TARGET" 2>/dev/null || true

echo
echo "── 생성 ──"
for key in $GENERATED; do
  if [ -n "$(read_value "$key")" ]; then
    printf '  =  %-28s 이미 값이 있다 — 건너뛴다\n' "$key"
  else
    write_value "$key" "$(generate "$key")"
    printf '  +  %-28s 생성했다\n' "$key"
  fi
done

# --------------------------------------------------------------------------------------
# 손으로 넣어야 하는 것 · 눈으로 확인해야 하는 것
# --------------------------------------------------------------------------------------
echo
echo "── 확인 ──"

if [ -z "$(read_value OPENAI_API_KEY)" ]; then
  printf '  !  %-28s 직접 넣어라 — platform.openai.com (지출 한도도 함께 걸어라)\n' OPENAI_API_KEY
else
  printf '  =  %-28s 값 있음\n' OPENAI_API_KEY
fi

# ⚠️ CORS 는 "비어 있지 않다" 로는 부족하다. 오타가 나도 기동은 되고, 증상은 배포 뒤에
#    프론트에서 **모든 요청이 차단되는** 형태로만 나타난다. 그래서 값을 눈에 보여준다.
cors=$(read_value CORS_ORIGINS)
printf '  ?  %-28s %s\n' CORS_ORIGINS "${cors:-(비어 있다)}"
echo '     └ 프론트 도메인이 맞는지 눈으로 확인해라. 스킴(https://)까지 정확해야 하고'
echo '       와일드카드(*)는 금지다 — allow_credentials=True 와 함께면 위험하다 (09 §7.7).'

# --------------------------------------------------------------------------------------
# compose 가 뜰 수 있는 상태인가
# --------------------------------------------------------------------------------------
missing=''
for key in $REQUIRED; do
  [ -n "$(read_value "$key")" ] || missing="$missing $key"
done

echo
if [ -n "$missing" ]; then
  echo "⛔ 아직 비어 있다:$missing"
  echo "   compose 는 \${VAR:?} 로 이 값들을 요구한다 — 채우기 전에는 기동하지 않는다."
  exit 1
fi

echo "✅ 필수 8개가 모두 찼다. 기동할 수 있다:"
echo
echo "   docker compose --env-file $TARGET_ARG -f docker-compose.prod.yml up -d --build"
echo
echo "⛔ $TARGET_ARG 는 커밋하지 마라 (.gitignore 의 .env.* 가 막고 있다)."
