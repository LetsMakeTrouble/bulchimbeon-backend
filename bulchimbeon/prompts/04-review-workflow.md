# M4 — 담당자 확인 워크플로 (차별점의 심장 ⭐)

@docs/02-business-rules.md §3·§4·§5·§9, @docs/05-api-contract.md §6·§7·§9, @docs/04-data-model.md (review_cards/feedbacks/official_qas) §6·§6.1·§7 를 기준으로 확인 카드 워크플로를 구현해줘.

> **절대 컷 불가 마일스톤이다** (결정 1.18). "답이 없을 때 시작되는 워크플로"가 차별점 그 자체다.

## 작업

1. **모델·마이그레이션**: review_cards, feedbacks
   - `review_cards.reason` = **`green` \| `yellow` \| `red` \| `feedback` \| `doc_update` \| `failed`**
     (🟢과 파이프라인 실패도 카드를 만든다 — 큐가 최종 안전망이다)
   - **`review_cards.document_version_id uuid FK NULL`** — `reason='doc_update'`일 때 **새로 활성화된 버전**
     (재검토를 유발한 쪽)을 채운다. 이 값이 없으면 `bulk-keep`을 구현할 수 없다
   - `review_cards.question_struct` — `answers.question_struct`(M3 저장분)에서 복사
   - **`feedbacks` UNIQUE `(answer_id, user_id)`** — 유저당 1건. 재제출은 verdict가 달라도 409이며
     verdict 변경은 지원하지 않는다 (D12)

2. **카드 생성 연결** (M3 TODO 해소) — reason별로 전부 만든다
   | 판정 | reason | 알림·브리핑 |
   | --- | --- | --- |
   | 🟢 | `green` | **브리핑·알림 대상에서 제외.** 큐에는 항상 적재. `correct` 2건으로 `recommend_approve=true`가 되는 순간부터 브리핑 최상단에 등장 (룰 1·3) |
   | 🟡 | `yellow` | 브리핑 |
   | 🔴 | `red` | urgent면 즉시(DND 제외), 아니면 브리핑 |
   | 파이프라인 실패 | `failed` | `answer_id=NULL`, `draft_answer=null`, `question_struct=null`. 질문자에게 `answer.failed` 알림 (D23) |
   - **재사용 답변(`source='reused'`)은 카드를 만들지 않는다** (D11). 담당자가 이미 확정한 원문이다

3. **크로스체크 API** (계약서 §6 feedback)
   - `correct`/`different`, `different`는 note 필수, 같은 유저 재제출 409 `DUPLICATE_FEEDBACK`
   - **허용 상태는 `answer.state ∈ {draft, verified}`뿐이다.** `expired`·`rejected`·`under_review`에
     피드백을 시도하면 **409 `FEEDBACK_NOT_ALLOWED`** (D12)
   - `different` → answer(및 연결 공식 Q&A) `under_review` + 카드(`reason='feedback'`)
   - `correct` 2건 → 카드 `recommend_approve=true`
   - **`correct` 시 `answer.official_qa_id`가 있으면 해당 `official_qas.correct_count`를 증가**시킨다 (D22).
     재사용 답변에 들어온 "맞았다"가 원본 지식의 신뢰도로 환류되는 경로다
   - 확정(verified) 답변에도 `different` 허용 (룰 3 ⚠️)
   - **응답에 갱신된 `feedback_summary`를 항상 포함**한다 — 프론트 낙관적 업데이트의 전제이며
     `GET /questions/{id}` 재조회를 불필요하게 만든다

4. **카드 큐 API** (계약서 §7 전체)
   - **목록** — `05 §7`의 **큐 목록 아이템 스키마 그대로**:
     `{id, reason, status, is_urgent, recommend_approve, grade, question_preview_en, question_preview_ko,
     created_at, first_viewed_at}`. 정렬은 승인추천 → 긴급 → 오래된 순.
     **목록만으로 큐 화면이 완성되어야 한다**(상세 호출 불필요)
   - **상세** — 최초 조회 시 `first_viewed_at` 기록 + `card.viewed` 이벤트.
     상세는 목록 아이템의 상위 집합이며 `draft_answer.citations[]`는 §6 `citations[]`와 동일 스키마
     (`document_id`·`document_version_id` 포함)
   - **액션**: approve / edit / **answer-option** / keep / reject / defer
     - `POST /review-cards/{id}/answer-option` `{index}` — **선택지 탭 응답**("30초 컷" 서사의 실현 수단).
       동작은 `edit`과 완전히 동일하되 본문이 `question_struct.options[index]` 텍스트다.
       범위를 벗어난 index는 400 `VALIDATION_ERROR`, `question_struct`가 없는 카드는 409 `INVALID_CARD_ACTION`.
       응답에 `selected_option: {index, text}` echo 포함
     - `edit`: `content_en` → ko 번역 → **ko를 확정 원문으로 고정**(이후 재번역 금지, D5)
     - **`keep`은 재검토 카드(`feedback`·`doc_update`)에만 유효**하다. `green`/`yellow`/`red`/`failed`에
       호출하면 409 `INVALID_CARD_ACTION`
     - **`approve`는 `green`/`yellow`/`red`에만**, **`answer-option`은 `red`에만** 유효하다.
       `05 §7.1` 액션 매트릭스를 그대로 서버에서 강제한다
     - `defer`: `{until?}` 생략 시 **기본값 = 다음 `briefing_hour`**(담당자 `users.timezone` 기준, D15)
   - **처리 시 부수 효과** (액션 공통)
     - answer 상태 전이(`04 §6` 다이어그램 그대로)
     - **질문 상태 `held → answered` 전이** — approve/edit/answer-option으로 해소되면
       `questions.status='answered'`로 바꾸고 `question.status_changed` 이벤트를 발행한다.
       `held_info`는 이력용으로 남기되 `card_status`를 `resolved`로 갱신한다.
       `reject`로 해소되면 status는 `held`를 유지하고 `card_status`만 `resolved`가 된다 (`04 §6.1`, `05 §6`)
     - 공식 Q&A 편입(질문 임베딩 포함). `reject`는 미편입
     - 미해소 피드백 일괄 `resolved` + 응답에 `resolved_feedbacks` 건수 (룰 9 담당자 우선)
     - **이미 resolved면 409 `ALREADY_RESOLVED` — body에 기존 `resolution`과 `resolved_at`을 포함**한다.
       재시도한 클라이언트가 "내가 방금 한 것인지 남이 한 것인지"를 판별할 수 있어야 한다
     - **만료(`expired`) 답변의 카드 처리 시도는 409**다 (D13). `expired`는 종착 상태이며 확정할 수 없다
   - `bulk-keep`: `{document_version_id}` 문서 갱신 묶음 전체 유지 (`reason='doc_update'` 전용)

5. **재검토 연쇄 구현** (M2 TODO 해소)
   - **버전 활성 전환** 시 해당 문서 근거 확정 답변·공식 Q&A → `under_review` +
     카드(`reason='doc_update'`, **`document_version_id` = 새로 활성화된 버전**) +
     `answers.review_cascade` 이벤트 + `review_cascade_count` 반환
   - **문서 soft delete도 동일한 연쇄를 일으킨다** (D20). 근거가 바뀌는 것과 사라지는 것은 답변 입장에서 같은 사건이다.
     그 문서에서 파생된 공식 Q&A는 **`archived`**로 내리고 `official_qa.archived` 이벤트를 남긴다
   - **공식 Q&A가 `under_review`로 내려갈 때, 그것을 `official_qa_id`로 참조하는 `source='reused'` 답변도
     함께 `under_review`로 내리고 질문자에게 알린다** (D21). 재사용으로 퍼진 답변이 원본과 따로 놀면 안 된다
   - 교훈 `needs_recheck=true` 처리는 M6 (`# TODO(M6)`)

6. **담당자 교체 이관** (M1 TODO 해소): pending/deferred 카드 신규 담당자 이관

7. **공식 Q&A API** (계약서 §9): 목록/검색, 상세, **`DELETE /official-qas/{id}`(담당자 전용) → `status='archived'`**
   (물리 삭제 아님). `archived`는 재사용·유사 첨부·검색에서 완전 제외되며 MVP에서는 되돌리지 않는다.
   전이 시 `official_qa.archived` 이벤트

## 완료 기준 — 시나리오 B 전 구간 e2e 테스트

질문 → 🔴 → 카드 생성 → `edit`(영어) → ko 번역 확정 → 공식 Q&A 편입 →
**같은 질문 재접수 → 재사용 즉답(확정 ko 원문 그대로, 재번역 안 됨 검증)**

**⚠️ 질문자 관점 GET을 반드시 단언한다 (여기가 데모에서 처음 터지는 지점이다)**
- 카드 확정 **직후** `GET /questions/{id}`가
  `status == "answered"` · `answer != null` · `answer.state == "verified"` ·
  `held_info.card_status == "resolved"` 를 만족하는지 확인한다.
- `held → answered` 전이를 빠뜨리면 질문자 화면에는 영원히 "보류 중"만 남는다.
  카드 큐 쪽 테스트만으로는 이 결함이 **절대 드러나지 않는다.**
- `reject`로 해소한 경우도 별도로 단언한다: `status == "held"` 유지 + `held_info.card_status == "resolved"`.

추가 테스트
- `different` → 재검토 → `keep` 복귀 / **`green`·`yellow` 카드에 `keep` 호출 시 409 `INVALID_CARD_ACTION`**
- 카드 중복 처리 409 — **body에 기존 `resolution`·`resolved_at`이 들어 있는지 확인**
- `correct` 2건 → `recommend_approve` / **재사용 답변에 `correct` → 원본 `official_qas.correct_count` 증가**
- **`answer-option`**: 정상 확정 + 범위 밖 index 400 + `red`가 아닌 카드에 호출 시 409
- 피드백 허용 상태: `under_review`·`expired`·`rejected` 답변에 피드백 시 409 `FEEDBACK_NOT_ALLOWED`
- 문서 재업로드·활성화 → 재검토 연쇄 N건 + **카드의 `document_version_id`가 새 버전으로 채워졌는지**
- **문서 soft delete → 동일한 재검토 연쇄 + 파생 공식 Q&A `archived`**
- **공식 Q&A `under_review` → 그것을 참조하는 재사용 답변도 `under_review`** (D21)
- **🟢 카드가 큐에는 있고 알림은 없는지** (브리핑 제외 검증은 M6)
- **만료 답변의 카드 처리 시도 → 409** (M3에서 넘어온 `06 §5` 테스트 7의 e2e 단언)
- asker의 카드 접근 403

`uv run pytest` 통과 → 커밋 `feat(M4): review workflow and knowledge loop`
