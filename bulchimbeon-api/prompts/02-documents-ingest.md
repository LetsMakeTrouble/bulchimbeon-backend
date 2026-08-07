# M2 — 문서 업로드 · 버전 · 인제스트

@docs/05-api-contract.md §4·§12.3, @docs/04-data-model.md (documents/document_versions/chunks) §7, @docs/06-ai-pipeline.md §1, @docs/02-business-rules.md §5 를 기준으로 문서 도메인을 구현해줘.

## 작업

1. **모델·마이그레이션**: documents, document_versions(활성 1개 부분 UNIQUE), chunks + HNSW 인덱스
   - 벡터 컬럼은 **`vector(1536)` 리터럴 고정**이다. `vector(EMBEDDING_DIM)`처럼 env를 컬럼 차원에 끌어오지 않는다.
     `EMBEDDING_DIM` env는 (a) 임베딩 **응답 차원 검증**, (b) OpenAI `dimensions` 파라미터 전달용이며
     불일치 시 기동에서 fail-fast한다 (`04` 문서 상단).
   - 인덱스: `USING hnsw (embedding vector_cosine_ops)`.
     생성 전 **`SELECT extversion FROM pg_extension WHERE extname='vector'`로 0.8.0 이상**임을 확인한다
     (M3의 `hnsw.iterative_scan` 전제. M-1에서 이미 확인했다면 재확인만).
2. **LLM 프로바이더 기반 작업 시작**: `services/llm/base.py`에 `LLMProvider` 프로토콜
   (`embed(texts) -> list[vec]`, `complete_json(system, user, schema) -> dict`, `translate(text, source, target) -> str`),
   `openai_provider.py`(임베딩만 이번에 실구현), `fake_provider.py`(결정적 임베딩 — 텍스트 해시 기반 벡터).
   `LLM_PROVIDER` env로 선택
3. **파일 저장**: `STORAGE_DIR/{project_id}/{version_id}/{filename}` 저장, 20MB 제한,
   확장자 화이트리스트(md/txt/pdf/docx) — 위반 시 400 `UNSUPPORTED_FILE_TYPE`.
   `STORAGE_DIR`는 절대 경로 전제다 (`03 §5.2`)
4. **인제스트 파이프라인** (`services/pipeline/ingest.py`, BackgroundTasks):
   - **BackgroundTasks에는 `version_id: UUID`만 넘긴다.** ORM 객체·`AsyncSession` 전달 금지 —
     FastAPI 0.106.0부터 `yield` 의존성이 태스크보다 먼저 정리되어 요청 세션은 이미 닫혀 있다.
     태스크 안에서 `async with AsyncSessionLocal() as db:`로 자체 세션을 열고,
     **실패를 기록할 때도 또 다른 새 세션**을 쓴다 (`03 §2` 원칙 4)
   - 파싱: MD/TXT 직접, PDF pypdf, DOCX python-docx (동기 파싱은 `run_in_executor`)
     - ✂️ **컷라인**: 시간이 부족하면 **MD/TXT만** 구현한다(결정 1.18 컷라인 5).
       시드 문서가 전부 MD이므로 데모에 영향이 없다. PDF/DOCX는 업로드 시 400으로 막고 TODO를 남긴다
   - 청킹: 헤딩 경로 우선 → 800토큰 초과 시 15% 오버랩 분할, meta에 heading_path/page_no
   - 임베딩 배치(100개) → chunks 저장 → `ingest_status=ready` (실패 시 failed + ingest_error)
   - 완료·실패 시 **SSE `document.ingested` `{document_id, version_id, status}` 발행**
     (`05 §12.3`. status는 `ready` \| `failed`). M5 이전이라 `sse_manager`가 없다면
     이벤트 발행 지점을 `notification`/`event` 서비스 훅으로 만들어 두고 M5에서 배선만 연결한다 —
     **폴링 규약만 남기고 이벤트를 생략하지 말 것**(프론트는 이 신호로 문서 목록을 갱신한다)
5. **API** (계약서 §4 전체): 업로드(`auto_activate` 기본 true), 재업로드=새 버전, 목록/상세,
   **활성 전환**, 원문 열람(`GET /documents/{id}/versions/{vid}/content` — §6 `citations[]`의
   `document_id`·`document_version_id`로 구성되는 근거 열람 URL이다), soft delete

   **활성 전환은 부분 UNIQUE 2문 스왑으로 한다** (`04 §7`). 단일 `UPDATE … CASE` 스왑은 `23505`로 죽는다.
   ```sql
   BEGIN;
   SELECT id FROM documents WHERE id = :doc_id FOR UPDATE;                        -- ① 상위 행 잠금
   UPDATE document_versions SET is_active=false WHERE document_id = :doc_id;      -- ② 전부 내린다
   UPDATE document_versions SET is_active=true  WHERE id = :new_version_id;       -- ③ 하나만 올린다
   COMMIT;
   ```
   재검토 연쇄는 M4에서 해소한다 — 지금은 `review_cascade_count: 0` 반환 + `# TODO(M4)` 주석.
6. **soft delete도 같은 재검토 연쇄를 일으킨다 (D20)** — `# TODO(M4): soft delete → 재검토 연쇄 + 파생 공식 Q&A archived`
   주석을 M4로 인계한다. 근거가 **바뀌는 것**과 **사라지는 것**은 답변 입장에서 동일한 사건이다.
   지금 당장 구현할 것: **검색 대상은 `documents.status='active'` 문서의 활성 버전 청크로 한정**한다는
   전제를 retrieval 쪽 헬퍼 시그니처에 못 박아 둔다(청크는 물리 삭제하지 않는다).
7. 이벤트: `document.version_activated`

## 완료 기준

- 테스트: 4개 포맷 각각 업로드 → ready 전환 → chunks 존재·heading_path 확인 (FakeLLM 임베딩)
- 재업로드 시 version_no 증가·기존 버전 보존, **활성 전환이 2문 스왑으로 성공**하고 활성 버전이 항상 1개
- 대용량(20MB 초과)·비허용 확장자 400
- `document.ingested` 이벤트가 ready·failed 양쪽에서 발행됨 (M5 전이면 훅 호출 여부를 목으로 검증)
- soft delete된 문서의 청크가 검색 대상에서 제외됨 (retrieval 헬퍼 단위 테스트)
- `uv run pytest` 통과 → 커밋 `feat(M2): document ingest pipeline`
