# M7 — 이력 타임라인 · 지표 대시보드 API

@docs/02-business-rules.md §10, @docs/05-api-contract.md §7·§13, @docs/04-data-model.md §3·§5(이벤트 타입·지표 매핑) 를 기준으로 구현해줘.

> ✂️ **컷라인 (결정 1.18 컷라인 6)** — 시간이 부족하면 지표를 **`auto_answer_rate` + `saved_wait_hours` 2종**으로
> 줄인다(발표 마지막 화면이 이 둘이다). 단 **1번 이벤트 기록 감사는 컷하지 않는다** —
> 이벤트가 비면 나중에 지표를 되살릴 수 없고, 과거 데이터는 소급 생성되지 않는다.

## 작업

1. **이벤트 기록 감사(audit)**: `04 §5`의 이벤트 타입 목록과 실제 기록 지점을 대조해 누락 보완.
   지표의 원천이므로 아래는 **필수**다 — 하나라도 비면 해당 지표의 분자 또는 **분모**가 만들어지지 않는다.
   | 이벤트 | 없으면 깨지는 것 |
   | --- | --- |
   | `question.graded`(grade, matching_rate, elapsed_ms) | 자동응답률·등급 분포 전부 |
   | `card.viewed` | 카드 처리 30초 비율의 **분모** |
   | `answer.reused` | 재질문 즉답률의 분자 |
   | **`answer.reuse_missed`**(payload: `best_similarity`) | 재질문 즉답률의 **분모** — 없으면 항상 1.0이 되어 지표가 무의미해진다 |
   | `feedback.created`(verdict) | 정정률 |
   | `question.status_changed`(from, to) | 타임라인의 `held → answered` 구간 |
   | `lesson.approved` · `official_qa.created` | 시계열의 학습 곡선 |

2. **타임라인 API** (계약서 §13): `GET /projects/{id}/events?entity_type=&entity_id=&limit=` —
   질문 단위 조회 시 연결된 답변·카드 이벤트를 함께 포함한다(질문의 전체 여정이 한 타임라인으로).
   응답 아이템은 `{type, actor: {id,name}|null, payload, created_at}`

3. **지표 API** `GET /projects/{id}/metrics?days=30` (계약서 §13 응답 구조 그대로, 룰 10 구현 노트 준수)
   - `auto_answer_rate`: `{value, target, green, yellow, red}` — 🟢+🟡 / 전체
   - `correction_rate`: `{value, target, correct, different}`
   - `card_handle_30s_rate`: `{value, target, within_30s, viewed_cards}`.
     **분모는 `first_viewed_at`이 기록된 카드 수**다. `viewed_cards == 0`이면 `value: null`
   - **`requestion_instant_rate`**: `{value, target, reused, reuse_missed}` —
     **`value = reused / (reused + reuse_missed)`** (D26). 분모 0이면 `value: null`.
     ⚠️ `reuse_missed`를 분모에 넣지 않으면 재사용이 1건만 일어나도 100%가 되어 실패를 은폐한다
   - **`grade_accuracy[]`**: 등급별 **`(verified 또는 correct 피드백 ≥1건인 답변 수) / 해당 등급 전체 발행 수`** (D25).
     아이템 shape은 `{grade, verified_rate, sample, window_days, sufficient, message}`이며
     **표본 30건 미만이면 `sufficient:false` + `verified_rate:null` + `message`**로 내린다.
     🟢·🟡·🔴을 **합산하지 않는다** — 등급별로 각각 낸다.
     M5의 `answer.accuracy_context` 헬퍼와 **동일 shape·동일 구현**을 공유한다(두 벌 만들지 말 것)
   - **`saved_wait_hours`**: `{value, assumption_hours, basis_count}` —
     `assumption_hours = settings.saved_wait_assumption_hours`(기본 24, **env 아님**),
     `basis_count` = 🟢+🟡로 즉답된 질문 수, `value = basis_count × assumption_hours`.
     **실측이 아니라 추정이므로 구조 자체가 가정치를 드러내야 한다** — 숫자 하나로 내리지 않는다
   - `?days=` 윈도우 파라미터, SQL 집계로 계산(이벤트 풀스캔 지양 — `events(project_id, created_at)` 인덱스 활용)

4. **시계열 API (신규)** `GET /projects/{id}/metrics/timeseries?days=30&bucket=day` (계약서 §13)
   - 아이템: `{date, questions, green, yellow, red, reused, lessons_approved, official_qas}`
   - `date`는 **담당자 `users.timezone` 기준** 버킷 시작일(`YYYY-MM-DD`)
   - **`official_qas`는 버킷 종료 시점 누적값**이다(증분 아님 — 지식이 쌓이는 곡선을 그린다)
   - `bucket`: `day` \| `week`, 기본 `day`
   - **질문이 없는 날도 빈 버킷을 0으로 채워서 내린다** (그래프에 구멍이 생기지 않게)
   - "쓸수록 좋아진다"를 그래프 하나로 보여주는 데이터다 — 발표 마지막 화면의 근거

5. 권한: metrics·events는 **멤버** 조회 가능

## ⚠️ 지표를 파괴하는 구현

- **`GET /review-cards/{id}`는 최초 조회 시 `first_viewed_at`을 기록한다.**
  목록에서 호버 프리페치·백그라운드 선행 조회를 하면 `card_handle_30s_rate`의 분모가 오염되어
  지표가 통째로 무의미해진다. 상세 호출은 **담당자가 실제로 카드를 열었을 때 정확히 1회**여야 한다.
  큐 목록 아이템(`05 §7`)에 렌더에 필요한 값이 전부 있으므로 상세를 미리 부를 이유가 없다.
  이 경고를 **API 계약서와 Swagger 설명 양쪽에** 남겨 프론트가 놓치지 않게 한다.
- 시드·테스트에서 카드 상세를 반복 조회하면 같은 오염이 생긴다. `first_viewed_at`은 **최초 1회만** 기록한다.

## 완료 기준

- 표본 이벤트 시나리오를 픽스처로 주입 → 각 지표 기대값 일치 단위 테스트
- **경계 테스트**: 피드백 0건(`correction_rate`) / `viewed_cards=0` → `value: null` /
  **`reused=0, reuse_missed=0` → `value: null`** / 표본 29건 → `sufficient:false`·`verified_rate:null`
- **`requestion_instant_rate` 분모 테스트**: `reuse_missed` 이벤트 2건 + `reused` 1건 → `value == 1/3`.
  (`reuse_missed`를 무시한 구현이면 1.0이 나와 여기서 잡힌다)
- **`saved_wait_hours` 구조 테스트**: `value == basis_count * assumption_hours`이고
  `assumption_hours`가 `projects.settings`에서 온 값인지 (설정을 바꾸면 응답이 따라 바뀌는지)
- **시계열 테스트**: 질문 없는 날이 0으로 채워지는지 / `official_qas`가 **누적**으로 증가하는지 / `bucket=week` 동작
- 질문 1건의 타임라인이 `created → graded → status_changed → feedback → card.viewed → edited → verified`
  순서로 조회됨
- `uv run pytest` 통과 → 커밋 `feat(M7): events timeline and metrics`
