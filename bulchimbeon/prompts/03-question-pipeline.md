# M3 — 질문 파이프라인 (데모의 심장 ⭐)

@docs/06-ai-pipeline.md 전체, @docs/02-business-rules.md §1·§2·§4·§6, @docs/05-api-contract.md §6, @docs/04-data-model.md (questions/answers/answer_citations/official_qas) §6·§6.1·§7 를 기준으로 질문→등급 확정 파이프라인을 구현해줘.

> 절대 컷 불가 마일스톤이다. 아래 규약은 하나라도 빠지면 첫 실행에서 재현성 있게 깨진다.

## 작업

1. **모델·마이그레이션**: questions, answers, answer_citations, official_qas
   (M4에서 쓰기 시작하지만 재사용 검사를 위해 테이블·검색은 지금 구현)
   - `answers`에 **`held_reason`**(`conflict`\|`no_evidence`\|`low_confidence`\|`schema_failed`\|`quota_exceeded`),
     **`question_struct jsonb`**(⑦ 결과 — M4에서 카드로 복사), **`sim_raw float`**(리스케일 전 top-1 원시 코사인) 포함
   - `answers`에 **`UNIQUE(question_id)`** — 질문당 1행. MVP는 답변 재생성 API를 제공하지 않는다
   - 벡터 컬럼은 `vector(1536)` 리터럴 고정 + HNSW `vector_cosine_ops`

2. **LLM 프로바이더 완성** — 호출 규약을 프로바이더 한 곳에서 강제한다
   - `complete_json`(strict 구조화 출력 + Pydantic 검증 + 재시도 2회), `translate`
   - **금지 파라미터 화이트리스트**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed` 를
     **전달하지 않는다.** GPT-5 계열에 넘기면 400이다. `max_tokens`가 아니라 **`max_completion_tokens`**
   - `LLM_REASONING_EFFORT=minimal`, `LLM_TIMEOUT_SECONDS=45`(15초는 추론 모델에 비현실적),
     `LLM_PIPELINE_DEADLINE_SECONDS=25`
   - **`LLM_MODEL_VERIFY`를 `LLM_MODEL_ANSWER`와 분리해 받는다.** ⑤ 근거 검증은 이 모델로 호출한다 —
     생성과 검증이 같은 호출이면 "자기평가 순환논리"가 되고, env 분리가 그 반박의 유일한 근거다
   - **호출 방식을 하나로 고정한다**: `client.responses.parse(model=…, input=…, text_format=SentencesOut)`
     또는 Chat Completions의 `response_format={"type":"json_schema","json_schema":{…,"strict":True}}`.
     **두 방식을 섞지 않는다.** M-1 판정 3에서 통과한 쪽을 쓴다
   - `fake_provider`는 프롬프트 내 마커 키워드로 결정적 출력(§5 테스트 시나리오를 전부 재현 가능하게)

3. **strict 스키마 규약** (④·⑤·⑦ 전부 해당) — M3 첫 실행에서 100% 재현되는 400의 원인이다
   - **모든 필드 required**, 모든 object에 `additionalProperties: false`
     (Pydantic `model_config = ConfigDict(extra="forbid")`), optional은 `Optional[X]`
   - **기본값 있는 필드 금지.** `conflict_chunk_ids: list[str] = []` 가 400의 직접 원인이다
   - `minItems`/`uniqueItems` 등 미지원 키워드는 스키마에 넣지 말고 **Pydantic validator로 사후 검증**
   - **인용 id는 프롬프트 지역 별칭**(`"ch-1"`, `"ch-2"`)을 쓰고 **UUID를 노출하지 않는다**.
     응답의 `chunk_ids` 중 `[EVIDENCE]`에 없는 id는 제거하고 그 문장을 **`supported=false`로 처리**한다
   - ④ 출력 스키마의 각 문장에 **`text_ko` 필드를 포함**해 ⑧의 ko 번역 호출을 제거한다(4회 → 3회)
   ```python
   class Sentence(BaseModel):
       model_config = ConfigDict(extra="forbid")
       text_en: str
       text_ko: str            # ⑧ 번역 호출 제거용
       chunk_ids: list[str]    # 기본값 금지

   class SentencesOut(BaseModel):
       model_config = ConfigDict(extra="forbid")
       sentences: list[Sentence]
       not_answerable: bool
       conflict: bool
       conflict_chunk_ids: list[str]   # 기본값 금지 — 모델이 항상 채운다
   ```
   - ⚠️ strict에서는 스키마 위반이 사실상 나지 않는다. `schema_failed` 경로는 refusal·max token 초과에서만
     도달하므로 **테스트 6은 FakeLLM 전용 경로**다(실LLM으로 재현하려 하지 말 것)

4. **파이프라인** (`services/pipeline/answer.py` — `06 §2`의 ①~⑧ 단계)

   ① 번역+`suggest_urgent` → ② 재사용 검사 → ③ 근거 검색 → ④ 인용 강제 생성 →
   ⑤ 근거 검증(G) → ⑥ min(S,G) 등급 분기 → ⑦ 🔴 질문 구조화 → ⑧ 발행

   - **② 재사용 2차 게이트 (단일 임계값 의존 제거)**
     - 원시 코사인 ≥ `reuse_threshold` 는 **후보**일 뿐이다. LLM에 *"이 두 질문은 같은 질문인가?"* 를
       **yes/no로 1회** 물어 `yes`일 때만 재사용한다
     - `no`이거나 임계값 미달이면 일반 생성 경로로 내려보내고 **`answer.reuse_missed`
       (payload: `best_similarity`) 이벤트를 반드시 기록**한다 — 재질문 즉답률의 **분모**다 (D26)
     - `under_review`·`archived` 공식 Q&A는 재사용 대상 제외 (D7)
     - 재사용 시: `source='reused'`, **`state='verified'`**, `expires_at=NULL`, **카드 미생성**,
       `matching_rate=NULL`, 확정 당시 **한국어 원문 그대로**(재번역 금지).
       `question.graded`는 `grade=green, matching_rate=null, source=reused`로 발행 (D11)
   - **③ 검색 — 유사도는 직접 만든다**
     - `sim_raw = 1 - (embedding <=> :q)`. **`<=>`는 거리다.** SQLAlchemy 헬퍼명이 `cosine_distance()`인 것에
       속지 말 것 — 그대로 유사도로 쓰면 등급이 정확히 뒤집힌다
     - **대상은 `documents.status='active'` 문서의 활성 버전 청크로 한정한다.**
       **공식 Q&A는 ③의 근거 검색 대상이 아니다** (D24) — ②의 `similar_official_qa`(`similar_threshold`~`reuse_threshold`)
       첨부 경로로만 노출된다. 본문 임베딩 컬럼·EVIDENCE 포맷·citation 렌더링이 미비하므로 범위를 줄이는 것이 옳다
     - `retrieval_top_k`는 **6**이다(8 아님)
     - 검색 트랜잭션에서 `SET LOCAL hnsw.iterative_scan='relaxed_order'; SET LOCAL hnsw.ef_search=100;`
       (HNSW post-filtering 대응, pgvector **0.8.0 이상** 필요)
     - **결과 0건이면 `iterative_scan`을 켠 재조회를 1회** 한 뒤에도 0건일 때만 `no_evidence`로 확정한다
     - `sim_raw`를 `answers.sim_raw`에 기록한다
   - **S·G·매칭률** (`services/pipeline/grading.py`)
     ```
     S = round(clamp((sim_raw - s_floor) / (s_ceil - s_floor), 0, 1) * 100)
     G = round(지지 문장 수 / 생성 시점 원본 문장 수 * 100)     # 분모 고정
     matching_rate = min(S, G)
     ```
     - **G의 분모는 항상 "생성 시점 원본 문장 수"다.** 무근거 문장을 제거해도 분모는 줄지 않는다 —
       프루닝은 발행 품질을 위한 조치이지 **매칭률을 되돌리지 않는다**. 분모를 프루닝 후 개수로 쓰면
       환각이 심할수록 등급이 올라가는 비단조 함수가 된다
     - **`grounding_min`(60)은 문장 3개 이상일 때만 적용**한다. **1~2문장 답변은 전 문장 supported 필수,
       하나라도 무근거면 🔴**(비율 통계가 의미를 갖지 못하는 구간)
     - `reuse_threshold`/`similar_threshold`는 **리스케일하지 않은 원시 코사인**에 적용한다. S와 섞지 않는다
   - **강제 🔴 4종**: `no_evidence`(검색 0건 **또는 top-1 `sim_raw < similarity_floor`**) ·
     `conflict` · `schema_failed`(재시도 2회 소진) · `quota_exceeded`(`settings.daily_llm_call_limit` 초과)
   - **⑥ DND 강등은 `low_confidence`만** (룰 6, D2). **강제 🔴 4종은 DND에서도 🔴을 유지한다** —
     충돌 감지 결과가 시각대 때문에 조용히 은폐되면 안 된다. 강등은 제거 후 남는 문장이 있을 때만 가능하다.
     **DND·브리핑 시각 판정은 현재 담당자(`projects.answerer_id`)의 `users.timezone` 단일 원천**이다
     (`settings.briefing_timezone`은 존재하지 않는다)
   - **⑦ 구조화 결과는 `answers.question_struct`에 저장한다.** M4에서 `review_cards.question_struct`로 복사한다
   - **⑧ 발행**: ④의 `text_ko`를 결합해 `content_ko`를 만든다(별도 번역 호출 없음).
     `state='draft'`, `expires_at = now + draft_expire_hours`, 인용 저장, `disclaimer` 포함
   - **④ 프롬프트에 반드시 넣을 문장**:
     *"A chunk that merely mentions the topic without stating the requested fact does NOT count as evidence.
     In that case set not_answerable=true."*
   - **`LLM_PIPELINE_DEADLINE_SECONDS` 초과 시** 그 시점까지의 결과로 🟡 발행 + 카드 생성(안전망)
   - 모든 임계값은 `projects.settings`에서 로드. 단계별 elapsed 로깅
   - **이벤트**: `question.created`, `question.status_changed`(payload: from/to — `04 §6.1`의 **모든 전이**),
     `question.graded`, `answer.published`, `answer.reused`, `answer.reuse_missed`.
     `question.graded`의 payload에는 **`S_raw`(=`sim_raw`) · `S` · `G_raw` · `G_final` · `removed_sentences` ·
     `grade` · `matching_rate` · `elapsed_ms`를 전부** 기록한다(환각 방어 2겹의 증적이자 캘리브레이션 재산출 근거)

5. **BackgroundTasks 규약** — 위반하면 질문이 `processing`에서 영구 정지한다 (`03 §2` 원칙 4)
   ```python
   # routers/questions.py — UUID만 넘긴다
   background_tasks.add_task(run_answer_pipeline, question_id)

   async def run_answer_pipeline(question_id: UUID) -> None:
       try:
           async with AsyncSessionLocal() as db:      # 태스크 자체 세션
               await _pipeline(db, question_id)
               await db.commit()
       except Exception as exc:
           async with AsyncSessionLocal() as db:      # 실패 기록은 반드시 새 세션
               await mark_question_failed(db, question_id, exc)
               await db.commit()
   ```
   - **ORM 객체·`AsyncSession`을 태스크에 넘기지 않는다.** FastAPI 0.106.0부터 `yield` 의존성이
     백그라운드 태스크보다 **먼저** 정리되므로 요청 세션은 태스크 실행 시점에 이미 닫혀 있다
   - **실패 기록도 반드시 새 세션**이다. 예외가 난 세션은 롤백 대상이라 그 위에서 커밋할 수 없다
   - 좀비 회수 잡(`processing` 5분 초과 → `failed`)은 M5 스케줄러에서 붙인다 — `# TODO(M5)` 주석

6. **API** (계약서 §6)
   - `POST /projects/{id}/questions` → 202 `{question_id, status, suggest_urgent, created_at}`.
     **asker 전용 — 담당자가 자기 프로젝트에 질문하면 403 `FORBIDDEN_ROLE`** (D17, M1의 `require_asker` 사용)
   - `PATCH /questions/{id}` — `urgency`만 변경. **허용 창은 `status='processing'` 동안뿐**이고
     그 후에는 409 `PIPELINE_IN_PROGRESS`(body에 현재 `status` 포함)
   - `GET /projects/{id}/questions` — **목록 아이템 스키마**(`05 §6`) 그대로:
     `{id, content_ko, status, grade, matching_rate, state, created_at, feedback_summary}`.
     발행된 답변이 없으면 `state`·`feedback_summary`는 `null`
   - `GET /questions/{id}` — 🟢🟡 / 🔴 `held_info` / `failed` `failure_info` / reused 네 형태 (계약서 예시 JSON 그대로).
     `citations[]`에 **`document_id`·`document_version_id`·`heading_path`·`page_no`** 포함(근거 원문 열람 URL 구성용)
   - 사용자 표시 문자열(`disclaimer`, `held_info.message`)은 **수신자 `users.language` 기준 서버 생성**이다 (`05 §1.5`)

7. **파이프라인 총 실패**: `question.status='failed'` + `failure_info` 응답 + `# TODO(M4): reason='failed' 카드 생성`.
   질문은 어떤 경우에도 사라지지 않는다 (D23)

## 완료 기준 — `06-ai-pipeline.md` §5 **테스트 1~7** 전부 구현·통과

> **8번(삭제 교훈 재생성 차단)은 M6 범위다.** M3에서는 ④ 프롬프트의 `[APPROVED LESSONS]` 자리만
> `# TODO(M6)`로 비워 둔다.

1. min(S,G) 강등 — **S 높음·G 낮음이 낮은 등급으로 떨어지는지**(리스케일 후 S 기준)
2. G<60 → 무근거 문장 제거·재계산, **분모는 그대로**여서 매칭률이 오르지 않음 / 전멸 시 🔴
3. conflict 강제 🔴 + `question_struct` 저장
4. 재사용 경계 — `reuse_threshold`(**M-1 산출값**)의 **±0.002** 지점을 검증한다.
   미만이면 생성 경로 + **`answer.reuse_missed` 발행**, 이상이면 2차 게이트 진입.
   ⚠️ **`0.919`/`0.921`을 하드코딩하지 말 것** — 산출값이 바뀌면 테스트도 함께 움직여야 한다
5. DND 분기 — `low_confidence`는 🟡 강등 / **강제 🔴 4종은 DND에서도 🔴 유지**
6. 스키마 3회 실패 → 🔴 `schema_failed` (FakeLLM 전용 경로)
7. 만료 답변 처리 — `expired`는 종착 상태이며 확정 대상에서 제외된다.
   **카드 처리 시도 → 409의 e2e 단언은 M4**에서 완성하고, M3에서는 서비스 계층 가드를 단위 테스트한다 (D13)

추가 검증
- **`similarity_floor` 테스트**: top-1 `sim_raw`가 floor 미만이면 강제 🔴 `no_evidence`
- **`1 - (<=>)` 회귀 테스트**: 동일 벡터의 `sim_raw`가 **1.0에 가깝게** 나오는지 SQL 레벨로 확인
  (부호가 뒤집혀 있으면 여기서 잡힌다 — FakeLLM만으로는 절대 검출되지 않는 결함이다)
- **BackgroundTasks 테스트**: 질문 접수 후 `processing`이 5분 이상 남지 않는지, 실패 시 `failed`로 기록되는지
- 실제 OpenAI 키가 `.env`에 있으면 `scripts/smoke_pipeline.py`(질문 1건 실행·등급·`elapsed_ms` 출력)로 1회 검증.
  키 없으면 스킵. **`elapsed_ms`가 `LLM_PIPELINE_DEADLINE_SECONDS` 안에 들어오는지 확인**하고
  M-1 판정 3의 추정치와 대조한다

`uv run pytest` 통과 → 커밋 `feat(M3): question answering pipeline with grading`
