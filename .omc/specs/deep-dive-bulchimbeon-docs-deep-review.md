# Spec: 불침번 문서 세트 결함 수정 — 확정 결정서

> deep-dive 트레이스(`.omc/specs/deep-dive-trace-bulchimbeon-docs-deep-review.md`)에서 확증된
> Blocker 10 + High 22 + 부수 Medium/Low를 **전수 수정**하기 위한 canonical 결정서.
> 문서 수정 작업자는 이 결정서를 유일한 기준으로 삼는다. 여기 없는 판단은 하지 않는다.

## Goal

불침번 백엔드 문서 세트(21개)를, Claude Code가 M0~M9를 순차 실행했을 때
(a) 데모 시나리오 A·B가 **시연 시각과 무관하게** 재현되고
(b) 구현 중 데이터 모델 마이그레이션·API 재배포를 유발하지 않으며
(c) 1주 일정 안에 완주 가능한 상태로 만든다.

## Constraints

- 문서만 수정한다. 코드는 `scripts/probe_calibration.py` 하나만 신규 생성.
- 문서 우선순위(`02 > 05 > 06 > 나머지`)를 유지하되, **02 내부 자기모순을 먼저 해소**한다.
- 기존 문서의 구조·톤·한국어 서술을 유지한다. 전면 재작성 금지, 최소 침습 수정.
- 프론트엔드 팀 일정·마일스톤은 **문서에 넣지 않는다**(사용자 결정: 범위 밖). 단 API 계약서의 프론트 갭은 전부 보강한다.

## Non-Goals

- 프론트엔드 일정/산출물/백업 데모 페이지 (범위 밖)
- 크로스인코더 rerank 도입 (1주 일정에 과함 — "확장 포인트"로만 언급)
- 복수 담당자, 언어 일반화, 매칭률 자동 보정

---

# 1. 확정 결정 (D-번호는 02 부록에 추가될 결정 노트 번호)

## 1.1 매칭률 산출 — 유사도 리스케일 도입 ★핵심

**문제**: 임계값 80/50/0.92/0.85가 `ada-002`(평균 코사인 0.85) 캘리브레이션인데 `text-embedding-3-small`(평균 0.43)에 이식됨. 🟢 도달 불가.

**확정 결정**:

```
① 원시 유사도  sim_raw = 1 - (embedding <=> query_vec)      # pgvector는 거리를 반환
② 리스케일      S = round(clamp((sim_raw - s_floor) / (s_ceil - s_floor), 0, 1) × 100)
③ 근거 점수     G = round(지지 문장 수 / 생성 시점 원본 문장 수 × 100)   # 분모 불변
④ 매칭률        matching_rate = min(S, G)
```

- `s_floor` / `s_ceil`은 **`projects.settings` 값**. 기본값 `s_floor: 0.25`, `s_ceil: 0.65` — **캘리브레이션 실측 후 확정하는 잠정값**임을 문서에 명시.
- 80/50 등급 임계값, 계약서 예시 JSON, 데모 대사("🟢 87%")는 **그대로 유지**된다. 리스케일이 스케일 차이를 흡수한다.
- **재사용/유사 임계값(`reuse_threshold`, `similar_threshold`)은 리스케일하지 않고 원시 코사인을 그대로 쓴다.** 이유: 계약서상 재사용 답변은 `matching_rate: null`이라 화면에 % 가 뜨지 않으므로 표기 모순이 없다. 기본값은 캘리브레이션 산출값으로 대체하며, 문서에는 `reuse_threshold: 0.92 (⚠️ 캘리브레이션 전 잠정값 — probe 실측 후 재설정)`로 표기.
- **재사용 2차 게이트 추가**: 원시 유사도가 `reuse_threshold` 이상이어도, LLM에 "두 질문이 같은 질문인가?" yes/no를 1회 물어 `yes`일 때만 재사용한다(+2초, $0.0002). 단일 임계값 의존 제거.
- **`similarity_floor`**: top-1 `sim_raw < s_floor`이면 근거 없음으로 보고 **강제 🔴 `no_evidence`**.

**캘리브레이션 게이트(신규 M-1)**: `scripts/probe_calibration.py`를 M0 착수 전에 1회 실행해 `s_floor`/`s_ceil`/`reuse_threshold`를 확정한다. 산출 표는 IR Deck 자료로 재사용.

## 1.2 G 분모 고정 — min(S,G) 복원

- `G`의 분모는 **항상 생성 시점 원본 문장 수**. 프루닝(무근거 문장 제거)은 발행 품질을 위한 것이며 **매칭률을 올리지 못한다**.
- `grounding_min(60)`은 **문장 3개 이상**일 때만 적용. 1~2문장 답변은 "전 문장 supported 필수, 아니면 🔴".
- 결과: 환각 60%(S90/G40) → 매칭률 40 🔴 로 정상 강등. 등급 함수 단조성 회복.

## 1.3 pgvector 거리/유사도

- 모든 검색 쿼리는 `1 - (embedding <=> :q)` 로 유사도를 만든다. SQLAlchemy 헬퍼명이 `cosine_distance()`임을 경고로 명기.
- HNSW post-filtering 대응: 검색 트랜잭션에서 `SET LOCAL hnsw.iterative_scan='relaxed_order'; SET LOCAL hnsw.ef_search=100;` (pgvector **0.8.0 이상** 필요 — 이미지·배포 DB 양쪽 `SELECT extversion FROM pg_extension WHERE extname='vector'` 확인).
- 결과 0건일 때는 `iterative_scan`을 켠 재조회 1회 후에도 0건일 때만 `no_evidence` 확정.
- `chunks`와 `official_qas`는 **각각 top-k를 뽑아 애플리케이션에서 병합**(단일 ORDER BY로 두 인덱스 동시 사용 불가).

## 1.4 🟢 확정 경로 — 02 자기모순 해소

- `review_cards.reason` enum에 **`green` 추가**.
- 🟢 답변도 카드를 **큐에 적재**하되 **브리핑·알림 대상에서 제외**한다. `correct` 2건 누적 시에만 `recommend_approve=true`가 되어 브리핑 최상단에 올라간다.
- `02 §1` 🟢 행 문구를 이에 맞게 교체. 룰 §3의 "승인 추천"이 성립하게 된다.

## 1.5 질문 상태 전이 신설

- `04`에 **§6.1 질문 상태 전이** 신설: `processing → answered | held | failed`, **`held → answered`**(카드 **approve / edit / answer-option**으로 해소 시. `reject`는 `held`를 유지하고 `card_status`만 `resolved`가 된다), `processing → failed`(파이프라인 실패), `failed → answered`(재처리 시 — MVP에서는 미지원, 카드 경로로만 해소).
  > ※ 본 항목은 같은 문서 §1.15(`answer-option` 신설)·§1.15(카드 액션 매트릭스 — `keep`은 `under_review`/`doc_update` 전용, 그 외 409)에 의해 supersede된다. `keep`은 red 카드에 호출 시 409이므로 held를 해소할 수 없다.
- `05 §6`에 명시: *"held 질문이 담당자 확정으로 해소되면 status는 answered로 전환되고 answer 객체가 채워진다. held_info는 이력용으로 유지하되 card_status를 resolved로 갱신한다."*

## 1.6 DND 강등 — 강제 🔴 제외 ★시연 시각 무관 보장

- **`conflict` / `no_evidence` / `schema_failed` / `quota_exceeded`는 DND에서도 🔴을 유지한다.** 강등 대상은 매칭률 미달(`low_confidence`)뿐이다.
- 근거: 충돌 감지 결과가 조용히 은폐되는 것은 룰 2("근거 없으면 보류") 위반보다 심각하다. 또한 이 규칙으로 **데모의 Q7·Q8이 시연 시각과 무관하게 🔴로 재현**된다.
- **타임존 단일 원천**: 브리핑·DND 시각 판정은 항상 **현재 담당자(`projects.answerer_id`)의 `users.timezone`**. `settings.briefing_timezone`은 **삭제**하고 `05 §8` 응답의 `timezone`은 파생값임을 주석. 담당자 교체 시 자동으로 따라간다.
- `prompts/09` 리허설 체크리스트에 "발표 시각 기준 DND 판정 결과 확인" 항목 추가.

## 1.7 시드 코퍼스 확장 + 충돌 픽스처 교정

- **청크 수를 20개 이상으로**: `api-spec.md`에 `Webhooks` / `Errors` / `Idempotency` / `Sandbox` / `Regions` 섹션 추가, `refund-policy.md`에 `Digital Goods` / `Partial Refunds` / `Disputes` 섹션 추가, 신규 문서 `seed/integration-guide.md`(4~5섹션) 추가.
- `retrieval_top_k` 기본값 **8 → 6**.
- **Q7 충돌 픽스처 교정** — `meeting-notes-2026-07.md`의 rate limit 문장을 진짜 모순으로:
  ```
  변경 전: Mike mentioned the rate limit will be raised to 100 requests/minute
           for enterprise keys, rollout date TBD.
  변경 후: Confirmed with Mike: the API rate limit is 100 requests per minute
           per API key. This supersedes the older figure in the API spec.
  ```
  시제·대상·확정성을 api-spec과 일치시켜야 어떤 모델도 충돌로 판정한다.
- **Q10 문안 조정**: `"일본 리전 환불 정책도 동일하게 적용되나요?"`(Q8과 어휘 근접) — 단, 1.1의 LLM 동일성 게이트가 주 방어선이므로 문안은 보조 수단.
- **Q9 기대값 완화**: `🔴 (no_evidence 또는 low_confidence)`. meeting-notes의 `"Payment gateway integration is on track."`은 **미끼 청크로 유지**(좋은 테스트).
- ④ 프롬프트에 추가: *"A chunk that merely mentions the topic without stating the requested fact does NOT count as evidence. In that case set not_answerable=true."*

## 1.8 LLM 호출 규약 (GPT-5 계열)

```env
LLM_MODEL_ANSWER=gpt-5-mini
LLM_MODEL_VERIFY=gpt-5-mini        # 신규 — ⑤ 근거검증 전용(생성과 분리 가능하게)
LLM_REASONING_EFFORT=minimal        # 20초 예산 달성 필수 조건
LLM_TIMEOUT_SECONDS=45              # 15초는 추론 모델에 비현실적
LLM_PIPELINE_DEADLINE_SECONDS=25    # 파이프라인 전체 데드라인
```
- **금지 파라미터**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed` — GPT-5 계열에 전달 시 400. 프로바이더에서 화이트리스트로 강제.
- `max_tokens` → **`max_completion_tokens`**.
- **호출 횟수 정정**: 🟢/🟡 경로 = ①번역 → ④생성 → ⑤검증 → ⑧ko번역 **4회 순차**, 🔴 경로 = ①번역 → ④생성 → ⑤검증 → ⑦구조화 4회. 임베딩 1회(②에서 산출해 ③ 재사용). 최악(④ 재시도 2회 포함) 6회.
- **최적화 확정**: ④ 생성 스키마의 각 문장에 `text_ko` 필드를 추가해 **⑧ ko 번역 호출을 제거**(4회 → 3회). 단 확정 원문 고정 규칙(D5)은 담당자 수정 경로에만 적용되므로 무관.
- **성능 목표 재기술**: `06 §0`의 "20초 이내"는 `reasoning_effort=minimal` 기준 **캘리브레이션 실측 후 확정**한다고 명시. `08 §4-1`의 대사 **"수 초 내"를 "20초 내"로 정정**.
- 데드라인 초과 시 현재까지 결과로 🟡 발행 + 카드 생성(안전망).

## 1.9 Structured Outputs strict 규약

- 호출 방식 고정: `client.responses.parse(model=…, input=…, text_format=SentencesOut)`. Chat Completions 사용 시 `response_format={"type":"json_schema","json_schema":{"name":…,"strict":True,"schema":…}}`. **두 방식을 섞지 않는다.**
- strict 요건: **모든 필드 required**, 모든 object에 `additionalProperties: false`(Pydantic `model_config = ConfigDict(extra="forbid")`), optional은 `Optional[X]`. **기본값 있는 필드 금지** — `conflict_chunk_ids: []`가 400의 직접 원인.
- 미지원 키워드(`minItems`/`uniqueItems` 등)는 Pydantic validator로 사후 검증.
- **인용 id 검증**: `[EVIDENCE]`에 없는 `chunk_ids`는 제거하고 해당 문장을 `supported=false` 처리. `chunk_ids`는 `["ch-1","ch-2"]` 형태의 **프롬프트 지역 별칭**을 쓰고 UUID를 노출하지 않는다.
- **주의 명시**: strict에서는 스키마 위반이 거의 없다. `schema_failed` 경로는 refusal·max token 초과에서만 도달하며 `06 §5 테스트 6`은 FakeLLM 전용 경로임을 문서에 적는다.

## 1.10 BackgroundTasks 규약

- 백그라운드 함수에는 **UUID만 전달**한다. ORM 객체·`AsyncSession` 전달 금지 (FastAPI 0.106.0부터 `yield` 의존성이 태스크보다 먼저 정리됨).
- 태스크 내부에서 `async with AsyncSessionLocal() as db:` 로 자체 세션 생성. **실패 기록도 새 세션으로.**
- **좀비 회수 잡 추가**: `status='processing' AND created_at < now() - interval '5 minutes'` → `failed` + 카드 생성(안전망).

## 1.11 단일 프로세스 전제 명시

- 배포 시작 커맨드에 **`--workers 1` 명시**. 이유(SSE in-memory 큐, APScheduler 중복)를 함께 기술.
- **중복 방지는 락이 아니라 제약으로**: `briefing_runs(project_id, run_date)` 테이블 신설 + `UNIQUE(project_id, run_date)` + `INSERT … ON CONFLICT DO NOTHING` → `rowcount == 1`일 때만 발송.
- 확장 시점의 정답(Redis Pub/Sub 또는 `LISTEN/NOTIFY`, 스케줄러 프로세스 분리, `pg_try_advisory_lock`)을 주석으로 남긴다.

## 1.12 부분 UNIQUE 스왑 절차

- 부분 UNIQUE 인덱스는 **DEFERRABLE 불가**. 전환은 **한 트랜잭션 안에서 2문으로, 순서를 지켜** 수행한다. 단일 UPDATE 스왑 금지.
  1. 활성 버전: `is_active=False` 일괄 → 대상만 `is_active=True`
  2. 담당자 교체: 구담당자 `role='asker'` → 신담당자 `role='answerer'` → `projects.answerer_id` 갱신 (같은 트랜잭션)
- 경합은 `SELECT … FOR UPDATE`로 상위 행(`documents`/`projects`)을 먼저 잠가 직렬화.

## 1.13 스키마 추가/변경 (04)

| 대상 | 변경 |
| --- | --- |
| `answers` | `held_reason text NULL` (`conflict\|no_evidence\|low_confidence\|schema_failed\|quota_exceeded`), `question_struct jsonb NULL` (M3에서 저장, M4에서 카드로 복사), `sim_raw float NULL`(실측 기록·캘리브레이션 근거) |
| `review_cards` | `reason` enum에 **`green`, `failed`** 추가, `document_version_id uuid FK NULL`(reason=doc_update일 때 **새로 활성화된 버전**) |
| `notifications` | `deliver_after timestamptz NULL` (NULL=즉시, 값=브리핑 보류) |
| `briefing_runs` | **신규** `(id, project_id, run_date, sent_at)` + `UNIQUE(project_id, run_date)` |
| `official_qas` | `status` enum에서 `archived` 유지 + **전이 규칙 신설**(`DELETE /official-qas/{id}` → archived, 문서 soft delete 시 파생 Q&A archived) |
| `answers` | `UNIQUE(question_id)` — **MVP에서 답변 재생성 API 미제공, 질문당 최대 1행** |
| `feedbacks` | UNIQUE를 `(answer_id, user_id)`로 변경 — 유저당 1건, 재제출 409, verdict 변경 미지원 |
| `chunks` / `official_qas` | `vector(1536)` **리터럴 고정**. `EMBEDDING_DIM` env는 응답 차원 검증 + `dimensions` 파라미터 전달용. 기동 시 불일치면 fail-fast |
| `projects.settings` | `s_floor`, `s_ceil`, `similarity_floor`, `daily_llm_call_limit`, `saved_wait_assumption_hours` 추가 / **`briefing_timezone` 삭제** / `retrieval_top_k` 8→6 |
| `events` | `answer.reuse_missed`(payload: `best_similarity`), `official_qa.archived`, `question.status_changed` 추가 |

## 1.14 상태·규칙 미정의 해소 (룰 문서)

| # | 쟁점 | 확정 |
| --- | --- | --- |
| D11 | 재사용 답변 상태 | `state=verified`, `expires_at=NULL`, 카드 미생성, 만료 스위퍼 대상 아님. `question.graded` 이벤트는 `grade=green, matching_rate=null, source=reused`로 발행 |
| D12 | 피드백 허용 상태 | `state ∈ {draft, verified}`만 허용. `expired`·`rejected`·`under_review`는 **409 `FEEDBACK_NOT_ALLOWED`** |
| D13 | 만료의 의미 | (1) 확정 불가(승인·수정 대상 제외), (2) 상태 표기. 재사용 풀은 원래 공식 Q&A뿐이므로 별도 배제 로직 불필요. `06 §5 테스트 7`을 **"만료 답변 카드 처리 시도 → 409"**로 교체 |
| D14 | 만료 스위퍼 조건 | `draft && expires_at < now && 연결된 review_cards 중 pending/deferred가 없을 것` — 살아 있는 카드 밑에서 답변을 죽이지 않는다 |
| D15 | `defer` 기본 만기 | `deferred_until` 기본값 = 다음 `briefing_hour`. 요청 body에 `{until?}` 선택 파라미터 |
| D16 | 담당자 교체 절차 | 1.12의 트랜잭션 순서. 구담당자는 `asker`로 강등되어 프로젝트에 남는다 |
| D17 | 담당자 질문 권한 | **담당자는 자기 프로젝트에 질문할 수 없다(403)**. 담당자는 교체 없이 프로젝트를 떠날 수 없으므로 **담당자 부재 상태는 발생하지 않는다** |
| D18 | 질문자 탈퇴 | `POST /projects/{id}/leave` 신설. `status='left'`, 기존 질문·답변·피드백은 보존 |
| D19 | 프로젝트 삭제 | MVP 범위 밖. 시드 리셋은 DB 레벨에서 수행 |
| D20 | 문서 soft delete | 새 버전 활성화와 **동일한 재검토 연쇄**를 일으킨다. 검색 대상은 `documents.status='active'`의 활성 버전 청크로 한정 |
| D21 | 재사용 답변 재검토 | 공식 Q&A가 `under_review`로 내려갈 때, `official_qa_id`로 그것을 참조하는 `source='reused'` 답변도 함께 `under_review` + 질문자 알림 |
| D22 | `correct_count` 갱신 | `answer.official_qa_id`가 있으면 "맞았다" 시 해당 `official_qas.correct_count++` |
| D23 | 파이프라인 실패 카드 | `reason='failed'`, `answer_id=NULL`, `draft_answer=null`. 알림 타입 `answer.failed` 추가 |
| D24 | 공식 Q&A 근거 범위 | **③ 검색 대상을 활성 버전 청크로 축소**. 공식 Q&A는 ②의 `similar_official_qa`(0.85~0.92) 첨부 경로로만 노출 — 본문 임베딩 컬럼·EVIDENCE 포맷·citation 렌더링이 전부 미비하므로 범위를 줄이는 것이 옳다 |
| D25 | 실측 정확도 정의 | 등급별 `(verified 또는 correct 피드백 ≥1건) / 해당 등급 전체 발행 수`. 표본 30 미만이면 값 대신 부족 표기 |
| D26 | 재질문 즉답률 | `answer.reused / (answer.reused + answer.reuse_missed)` — `reuse_missed` 이벤트 신설로 분모 확보 |

## 1.15 API 계약 보강 (05)

**추가/변경 엔드포인트**
- `POST /sse/ticket` → `{ticket}` (TTL 60초, 1회용). `GET /sse/stream?ticket=` — **access token을 URL에 싣지 않는다**
- `PATCH /questions/{id}` — urgency 변경 (표에 정식 등재, 요청/응답 스키마 명시)
- `POST /review-cards/{id}/answer-option` `{index}` — **선택지 탭 응답**("30초 컷" 서사의 실현 수단)
- `POST /projects/{id}/leave` — 질문자 자진 탈퇴
- `DELETE /official-qas/{id}` — 담당자 전용, → archived
- `GET /projects/{id}/metrics/timeseries?days=30&bucket=day` — 학습 곡선용 시계열

**응답 shape 명시(현재 누락)**
- `GET /review-cards` **목록 아이템**: `{id, reason, status, is_urgent, recommend_approve, question_preview_en, question_preview_ko, grade, created_at, first_viewed_at}`
- 브리핑 배열 shape 통일: `recommend_approve` / `pending_cards` / `deferred_cards` / `doc_review_bundles[].cards` 모두 **위 목록 아이템 스키마 사용**(N+1 제거)
- `GET /projects/{id}/questions` **목록 아이템**: `{id, content_ko, status, grade, matching_rate, state, created_at, feedback_summary}`
- `status: "failed"` 응답 형태: `failure_info: {reason, message, card_status}`
- `POST /answers/{id}/feedback` 응답에 **갱신된 `feedback_summary` 포함**(낙관적 업데이트 지원)
- 409 `ALREADY_RESOLVED` body에 **기존 `resolution`과 `resolved_at` 포함**(재시도 시 판별)

**필드 추가**
- `citations[]`에 **`document_id`, `document_version_id`, `heading_path`, `page_no`** 추가 → 근거 원문 열람 URL 구성 가능 (기능 2.2 복구)
- `held_info.reason` enum에 **`quota_exceeded`** 추가
- 202 응답에 `created_at` 추가(진행 시간 표시용)
- `metrics.saved_wait_hours`를 `{value, assumption_hours, basis_count}` 구조로 — 가정치임을 화면에 표기 가능하게
- `metrics.card_handle_30s_rate`에 분자·분모 추가

**규약 명시**
- **i18n 소유 주체**: 서버가 만드는 사용자 표시 문자열(`disclaimer`, `held_info.message`, 알림 `title`/`body`, 정정 사유)은 **수신자의 `users.language` 기준**으로 생성한다. 에러 `message`는 개발자용이며 한국어 고정, 프론트는 `error.code`로 자체 문안을 표시한다.
- **프리페치 금지 경고**: `GET /review-cards/{id}`는 `first_viewed_at`을 기록하므로 **목록에서 호버 프리페치하지 말 것**(지표 파괴).
- **카드 액션 매트릭스**: reason별 유효 액션 표를 명시. `keep`은 `under_review`/`doc_update` 카드에만, 그 외 호출 시 409.
- **SSE 재연결**: Railway는 SSE를 **15분에 강제 종료**(하트비트 없으면 5분). 재연결은 필연이므로 프론트는 재연결 시 구독 리소스를 **전량 재조회**한다. 서버는 스트림 시작 시 미읽음 수를 1회 push.
- **토큰 만료**: access 만료 시 서버가 스트림을 종료하고, 프론트는 refresh 후 새 ticket으로 재연결한다.
- `settings` PATCH 허용 키: `04 §3`의 전체 키 목록을 계약서에 명시.
- **SSE 이벤트 추가**: `document.ingested` `{document_id, version_id, status}` (인제스트 완료 — 현재 폴링 규약조차 없음)

## 1.16 환각 방어 4겹 명시 (D-1 해소)

`06`에 신설 섹션 — 발표 슬라이드가 그대로 인용할 표:

| 겹 | 단계 | 방어 방식 | 증적 필드 |
| --- | --- | --- | --- |
| 1 | 생성 차단 | 문장별 인용 강제 + strict 스키마 + 인용 id 실재 검증 | `answer_citations` |
| 2 | 발화 전 검증 | **`LLM_MODEL_VERIFY`(생성과 분리된 호출)**로 문장별 supported 판정, 무근거 문장 제거 | `events.payload.removed_sentences`, `G_raw`/`G_final` |
| 3 | 등급 필터 | `min(S,G)` + 강제 🔴(충돌/근거없음/스키마실패/한도초과) | `answers.matching_rate`, `held_reason` |
| 4 | 사후 검증 | 담당자 확정 + 질문자 크로스체크 + 실측 정확도 표기 | `feedbacks`, `metrics.grade_accuracy` |

- `LLM_MODEL_VERIFY`를 env로 분리해 **다른 모델로 교체 가능**하게 한다 → 예상 질문 *"매칭률 자기평가는 순환논리 아닌가?"* 에 대한 최강 답변.
- `events.payload`에 `S_raw`, `S`, `G_raw`, `G_final`, `removed_sentences`를 전부 기록.

## 1.17 개발 환경·인프라

- **Windows(PowerShell) 블록** 신설: `&&`는 PowerShell 5.1 파서 에러 → `cmd; if ($?) { cmd2 }` 로 표기. CLAUDE.md 명령어 블록도 동일 수정.
- **`STORAGE_DIR`는 절대 경로 권장**. 호스트 실행 모드와 컨테이너 실행 모드를 **섞지 않는다**(하나를 표준으로 고정 — 개발은 `docker compose up -d db` + 호스트 uvicorn 표준).
- `.gitattributes`에 `*.md text eol=lf`. `content_hash` 정규화 정의 명문화: `sha256(lesson.strip().lower().replace("\r\n","\n"))` + 연속 공백 단일화.
- **테스트 인프라 확정**: testcontainers **미도입**. 이미 떠 있는 Postgres에 붙는다(`TEST_DATABASE_URL`, `…/bulchimbeon_test`). 세션 픽스처에서 `CREATE EXTENSION IF NOT EXISTS vector` + `Base.metadata.create_all`, 테스트별 **트랜잭션 롤백 격리**. CI는 GitHub Actions `services: pgvector/pgvector:pg16`만. 마이그레이션 검증 테스트 1개만 `@pytest.mark.slow`로 분리.
- **Alembic + pgvector 함정 3종** 명시: (1) 최초 마이그레이션 첫 줄 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`, (2) `alembic/script.py.mako`에 `from pgvector.sqlalchemy import Vector` 추가(없으면 `NameError`), (3) `database.py`에서 asyncpg 커넥션 init에 `register_vector`.
- **SSE**: FastAPI 0.135.0+ 네이티브(`fastapi.sse.EventSourceResponse`)를 사용하고 `sse-starlette` 의존성 제거. FastAPI `>=0.135.0` 핀. (네이티브가 `X-Accel-Buffering: no`·15초 ping 자동 처리)
- **`python-jose[cryptography]>=3.4.0`** 으로 핀(CVE-2024-33663/33664). "또는 pyjwt" 양자택일 표현 제거.
- `daily_llm_call_limit`을 env가 아닌 **`projects.settings`**로 이동(하드코딩 금지 원칙 일관성).

## 1.18 빌드 플랜 재조정

- **M-1 캘리브레이션 게이트 신설**(M0 전, 15분): `scripts/probe_calibration.py` + SQL 보조 프로브 실행 → `s_floor`/`s_ceil`/`reuse_threshold` 확정, `reasoning_effort` 지원 여부·지연 확정, pgvector 버전 확인.
- **일차 재배치**: 배포(M9 일부)를 **6일차로 앞당긴다**. 외부 마찰(Railway pgvector extension 권한, 마이그레이션, SSE 프록시)을 하루 먼저 노출시켜 복구 시간을 확보.
  | 일차 | 목표 |
  | --- | --- |
  | 0 | **M-1 캘리브레이션 게이트** (15분) |
  | 1 | M0 + M1 |
  | 2 | M2 (MD/TXT 우선, PDF/DOCX 여유 시) |
  | 3~4 | M3 |
  | 5 | M4 |
  | 6 | M5 + **클라우드 배포 1차** |
  | 7 | M6 브리핑 API + M7 지표 + 시드·리허설 |
- **컷라인 재조정**(포기 순서, 데모 스크립트 1~6단계가 기준선):
  1. **M8 외부 연동 전부** — 컷라인이 아니라 **기본 제외**로 승격. `POST /integrations` 스텁 + 501만.
  2. 교훈 메모리(M6 후반) — 데모 미등장. 테이블·조회 API만.
  3. 알림 다국어 문안 — 한국어 고정 + `answer.corrected`만 양쪽 언어.
  4. 브리핑 스케줄러 → `GET /briefing/today` 수동만. **단 M5의 `deliver_after` 보류 로직도 함께 컷**(반쪽 상태 방지).
  5. M2 DOCX·PDF → MD/TXT만.
  6. M7 지표 → `auto_answer_rate` + `saved_wait_hours` 2종만.
  - 절대 컷 불가: **M-1, M3, M4** + M5의 SSE `answer.completed` + M9 시드.
- **M3 DoD 정정**: "테스트 8종" → **"테스트 1~7 (8번 삭제 교훈 재생성 차단은 M6 범위)"**. `07` M3 행도 동일 정정.

---

# 2. 파일별 작업 분배

| 담당 | 파일 | 반영할 결정 |
| --- | --- | --- |
| **A** | `docs/02-business-rules.md`, `CLAUDE.md` | 1.1(S정의) 1.2 1.4 1.6 1.14(D11~D26 부록) + CLAUDE.md "자주 틀리는 규칙" 갱신·PowerShell 명령어 |
| **B** | `docs/04-data-model.md`, `docs/03-tech-spec.md` | 1.3 1.8 1.10 1.11 1.12 1.13 1.17 + §6.1 질문 상태 전이 |
| **C** | `docs/05-api-contract.md` | 1.5(계약 문구) 1.15 전부 |
| **D** | `docs/06-ai-pipeline.md`, `docs/07-build-plan.md`, `docs/08-demo-scenario.md` | 1.1 1.2 1.3 1.7 1.8 1.9 1.16 1.18 |
| **E** | `prompts/*.md` (10개), `bulchimbeon/scripts/probe_calibration.py`(신규) | 각 마일스톤 지시를 위 결정에 맞춰 갱신 + M-1 프롬프트 신규 + 프로브 스크립트 |

# 3. Acceptance Criteria

- [ ] `02 §1`의 🟢 행과 `02 §3`의 승인 추천이 더 이상 모순되지 않는다
- [ ] `S` 산출식이 리스케일을 포함하고 `s_floor`/`s_ceil`이 settings에 있다
- [ ] `G` 분모가 "생성 시점 원본 문장 수"로 고정되어 프루닝이 매칭률을 올리지 못한다
- [ ] 모든 검색 서술이 `1 - (embedding <=> q)` 형태로 유사도를 만든다
- [ ] 강제 🔴 4종이 DND 강등 대상에서 제외됨이 룰과 파이프라인 양쪽에 있다
- [ ] `held → answered` 전이가 `04`와 `05`에 있다
- [ ] `review_cards.reason`에 `green`·`failed`가 있고 🟢 카드가 브리핑에서 제외됨이 명시되어 있다
- [ ] `citations[]`에 `document_id`/`document_version_id`가 있다
- [ ] `GET /review-cards` 목록 아이템 스키마가 계약서에 있다
- [ ] `temperature` 금지·`max_completion_tokens`·`reasoning_effort`·타임아웃 45초가 있다
- [ ] `--workers 1`과 `briefing_runs` UNIQUE가 있다
- [ ] BackgroundTasks에 UUID만 전달하는 규약과 좀비 회수 잡이 있다
- [ ] 시드 청크가 20개 이상이고 Q7 충돌 문안이 진짜 모순이다
- [ ] `07`에 M-1 게이트와 재배치된 일정·컷라인이 있다
- [ ] `scripts/probe_calibration.py`가 존재하고 판정 기준표가 함께 있다
- [ ] 문서 간 상호 참조(§번호·필드명)가 수정 후에도 일치한다

## Trace Findings (요약)

세 조사 레인이 서로 다른 방법론으로 **동일한 근본 원인**에 수렴: 임계값 세트가 `ada-002` 시대 캘리브레이션이며 `text-embedding-3-small`에서 🟢 도달 불가. 이로 인해 (a) 매칭률 3분기가 무력화, (b) `min(S,G)`의 G가 죽은 코드, (c) 자동응답률(🟢+🟡 합계)이 실패를 은폐, (d) FakeLLM 테스트가 동어반복이라 검출 불가, (e) 발견 시점이 발표 하루 전. 독립적으로 상태 모델에 3개의 폐쇄성 결함과 계약 필드 저장소 부재 3건이 존재. 전체 근거는 `deep-dive-trace-bulchimbeon-docs-deep-review.md`.
