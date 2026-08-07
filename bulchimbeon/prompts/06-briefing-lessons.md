# M6 — 아침 브리핑 · 교훈 메모리

@docs/02-business-rules.md §6·§7, @docs/05-api-contract.md §7·§8·§10, @docs/04-data-model.md §2(briefing_runs)·§3·§7, @docs/06-ai-pipeline.md §3(교훈 추출)·§4(스케줄러), @docs/03-tech-spec.md §5.2 를 기준으로 구현해줘.

> ✂️ **컷라인 (결정 1.18)** — 시간이 부족하면 아래 순서로 포기한다.
> 1. **교훈 메모리(4~7번)** — 데모 스크립트에 등장하지 않는다. `lessons` 테이블 + 목록 조회 API만 남기고
>    추출·주입·승인·삭제 차단은 TODO로 넘긴다(컷라인 2).
> 2. **브리핑 스케줄러(3번)** — `GET /briefing/today` 수동 호출만 남긴다(데모 플랜B와 동일, 컷라인 4).
>    **이때 M5의 `deliver_after` 보류 로직도 함께 컷한다.** 보류만 하고 flush가 없으면 알림이 영영 나가지 않는
>    반쪽 상태가 된다.
>
> 브리핑 **API(2번)** 자체는 컷하지 않는다 — 담당자 화면의 근간이다.

## 작업

1. **모델·마이그레이션**: lessons, **`briefing_runs(id, project_id, run_date date, sent_at)`**
   + **`UNIQUE(project_id, run_date)`**

2. **브리핑 API** (계약서 §8): `GET /projects/{id}/briefing/today` — 언제든 수동 호출 가능(데모 플랜B)
   - **모든 카드 배열은 `05 §7`의 큐 목록 아이템 스키마를 그대로 쓴다.**
     `recommend_approve` / `pending_cards` / `deferred_cards` / `doc_review_bundles[].cards`가
     **전부 동일 shape**이어야 한다 — 배열마다 필드가 다르면 프론트가 N+1 상세 호출을 하게 된다
     (`recommend_approve[]`에만 `correct_count`가 추가로 붙는다)
   - `doc_review_bundles[]`: `{document_id, document_version_id, title, new_version, affected_count, cards[]}`.
     `document_version_id`는 `bulk-keep`에 그대로 넘기는 값이다 (M4에서 카드에 채워 둔 컬럼)
   - `stats_snapshot`: `{auto_answer_rate, questions_24h}`
   - **`timezone`은 파생값이다** — 현재 담당자(`projects.answerer_id`)의 `users.timezone`을 그대로 내린다.
     `settings.briefing_timezone`은 **존재하지 않는다**. 담당자 교체 시 자동으로 따라간다
   - ⛔ **`reason='green'` 카드는 브리핑에 포함하지 않는다.** 단 `correct` 2건 누적으로
     `recommend_approve=true`가 되면 `recommend_approve[]` 최상단에 올라온다 (룰 1·3).
     큐(§7)에는 항상 적재되어 있다 — **큐와 브리핑의 필터가 다르다는 점이 이 마일스톤의 핵심**이다

3. **브리핑 스케줄러**: 매 정시 체크 잡 — 프로젝트별 `briefing_hour`(**담당자 `users.timezone`**, zoneinfo) 도달 시
   - **중복 발송은 락이 아니라 제약으로 막는다 (TOCTOU 방지)**
     ```sql
     INSERT INTO briefing_runs (id, project_id, run_date, sent_at)
     VALUES (:id, :pid, :run_date, now())
     ON CONFLICT (project_id, run_date) DO NOTHING;
     ```
     **`rowcount == 1`일 때만 실제 발송**한다. `SELECT`로 "오늘 보냈나?"를 먼저 확인하고 분기하는 방식은
     재시작·수동 트리거와 겹치면 두 번 나간다. `run_date`는 **담당자 타임존 기준 날짜**다
   - M5에서 `deliver_after`로 보류해 둔 알림을 일괄 flush
     (`card.created` 묶음은 `briefing.ready` 하나로 요약 가능)
   - `briefing.ready` 알림 + SSE
   - 확장 시점의 정답(스케줄러 프로세스 분리, `pg_try_advisory_lock`)은 주석으로만 남긴다 — 지금은 `--workers 1` 전제다

4. **교훈 추출** (M4 edit 처리에 연결): 원답 vs 수정답 diff → LLM 한 줄 교훈 → candidate 등록
   - **`content_hash` 정규화 정의 (이 정의를 벗어난 해시 계산을 만들지 않는다)**
     ```python
     content_hash = sha256(
         re.sub(r"\s+", " ", lesson.replace("\r\n", "\n").strip().lower())
     ).hexdigest()          # strip → lower → CRLF 정규화 → 연속 공백 단일화
     ```
     `.gitattributes`의 `*.md text eol=lf`(M0)와 짝이다. CRLF가 섞이면 OS마다 해시가 달라져
     삭제 교훈 재생성 차단(D8)이 무력화된다
   - 삭제된 교훈의 해시와 일치하면 후보 생성을 스킵한다

5. **교훈 주입** (M3 ④ TODO 해소): approved 교훈(최대 `settings.max_lessons`=30개)을 생성 프롬프트
   `[APPROVED LESSONS]`에 주입 + `last_used_at` 갱신. **candidate는 어디에도 쓰이지 않음을 검증**한다

6. **교훈 API** (계약서 §10): 목록(30개 초과 시 `cleanup_suggestions[]` — 오래되고 `last_used_at` 없는 순),
   approve, delete. 이벤트 `lesson.candidate` / `lesson.approved` / `lesson.deleted`

7. **문서 갱신 연동**: 재검토 연쇄(M4) 시 교훈 `needs_recheck=true` (자동 삭제 금지, 룰 5)

## 완료 기준

- 교훈 루프 e2e: edit 확정 → candidate 생성 → approve → 다음 질문 생성 프롬프트에 포함
  (FakeLLM으로 프롬프트 캡처 검증) → delete → **동일 내용 재추출 시 candidate 미생성**
  (= `06 §5` 테스트 8 "삭제 교훈 재생성 차단" — M3에서 이 마일스톤으로 넘어온 항목)
- **`content_hash` 정규화 테스트**: `"Japan is 20 days.\r\n"` / `"  japan  is 20 days. "` /
  `"Japan is 20 days."` 세 입력이 **같은 해시**를 내는지
- 브리핑: 시간 주입 테스트로 `briefing_hour` 도달 → 알림 flush + `briefing.ready` 발송
- **중복 발송 방지 테스트**: 같은 `run_date`로 발송 로직을 **연속 2회 호출**해도 알림이 1건만 생기는지.
  두 번째 호출이 `ON CONFLICT DO NOTHING`으로 `rowcount == 0`이 되어 조기 반환하는지 단언한다
- **🟢 카드 브리핑 제외 테스트**: `reason='green'` 카드가 큐 목록에는 **있고** 브리핑 응답에는 **없으며**,
  `correct` 2건 후에는 `recommend_approve[]`에 **나타나는지** — 세 단계를 모두 단언한다
- **브리핑 배열 shape 통일 테스트**: 네 배열의 아이템 키 집합이 `05 §7` 목록 아이템과 동일한지
  (`recommend_approve[]`의 `correct_count`만 예외)
- `recommend_approve` 카드가 브리핑 최상단 정렬
- 담당자 교체 후 브리핑 `timezone`이 신규 담당자의 `users.timezone`으로 따라가는지
- `uv run pytest` 통과 → 커밋 `feat(M6): briefing scheduler and lesson memory`
