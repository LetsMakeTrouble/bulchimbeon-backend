#!/usr/bin/env bash
# M-1 보조 프로브 — 배포 DB(Railway/Render) 검증.
#
# 00-calibration.md §3 이 요구하는 "배포 DB에서도 확인" 항목이다.
# 매니지드 Postgres 가 CREATE EXTENSION vector 권한을 주는지, 주더라도 0.8.0 이상인지를
# 배포 당일이 아니라 지금 확인한다 (03 §6).
#
# 사용법:
#   .env 에 DEPLOY_DATABASE_URL=postgresql://... 를 넣고
#   bash scripts/probe_deploy_db.sh
#
# psql 은 호스트에 설치하지 않고 pgvector 이미지의 것을 빌려 쓴다.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

if [ ! -f .env ]; then
  echo "  .env 가 없습니다." >&2
  exit 1
fi
set -a; . ./.env; set +a

if [ -z "${DEPLOY_DATABASE_URL:-}" ]; then
  echo "  DEPLOY_DATABASE_URL 이 .env 에 없습니다."
  echo "  Railway: Postgres 서비스 > Variables > DATABASE_PUBLIC_URL (내부용 DATABASE_URL 아님)"
  echo "  Render : New > PostgreSQL > External Database URL"
  exit 1
fi

echo "배포 DB 프로브 — 호스트: $(printf '%s' "$DEPLOY_DATABASE_URL" | sed -E 's#^.*@([^/?]+).*#\1#')"
echo

docker run --rm -i -e PGURL="$DEPLOY_DATABASE_URL" pgvector/pgvector:pg16 \
  psql "$PGURL" -v ON_ERROR_STOP=0 <<'SQL'
\echo '=== 1. CREATE EXTENSION vector 권한이 있는가? ==='
CREATE EXTENSION IF NOT EXISTS vector;

\echo '=== 2. 버전이 0.8.0 이상인가? (hnsw.iterative_scan 전제) ==='
SELECT extversion AS pgvector_version FROM pg_extension WHERE extname='vector';
SELECT version() AS server_version;

\echo '=== 3. hnsw.iterative_scan 을 실제로 설정할 수 있는가? ==='
SET hnsw.iterative_scan = 'relaxed_order';
SHOW hnsw.iterative_scan;

\echo '=== 4. <=> 는 거리인가? (기대 same=0, orthogonal=1) ==='
SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector AS same,
       '[1,0,0]'::vector <=> '[0,1,0]'::vector AS orthogonal;

\echo '=== 5. 부분 UNIQUE 스왑 — 로컬과 동일하게 순서 의존인가? ==='
CREATE TABLE IF NOT EXISTS _probe_t(id int primary key, doc int, act bool);
CREATE UNIQUE INDEX IF NOT EXISTS _probe_t_doc_idx ON _probe_t(doc) WHERE act;
DELETE FROM _probe_t;
INSERT INTO _probe_t VALUES (1,1,false),(2,1,true);
\echo '--- 역방향(높은 id -> 낮은 id): 23505 나야 로컬과 일치 ---'
UPDATE _probe_t SET act = (id = 1) WHERE doc = 1;
DROP TABLE _probe_t;

\echo '=== 6. vector(1536) 컬럼과 HNSW 인덱스를 만들 수 있는가? ==='
CREATE TABLE IF NOT EXISTS _probe_v(id int primary key, embedding vector(1536));
CREATE INDEX IF NOT EXISTS _probe_v_hnsw ON _probe_v USING hnsw (embedding vector_cosine_ops);
SELECT indexname FROM pg_indexes WHERE tablename='_probe_v';
DROP TABLE _probe_v;
SQL

echo
echo "결과 해석"
echo "  1 이 실패하면 -> 매니지드 DB 가 확장 생성을 막는다. 다른 제공자나 자체 호스팅이 필요하다 (03 §6)."
echo "  2 가 0.8.0 미만 -> hnsw.iterative_scan 사용 불가. 04 §7 의 post-filtering 대안이 필요하다."
echo "  5 가 23505 -> 로컬과 동일. 2문 스왑 절차가 배포 환경에서도 필수임이 확인된다."
