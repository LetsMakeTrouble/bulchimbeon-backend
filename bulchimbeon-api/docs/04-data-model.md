# 04. 데이터 모델

> 모든 PK는 UUID. 모든 테이블에 `created_at`(기본), 갱신되는 테이블에 `updated_at`.
> 벡터 컬럼은 `vector(1536)` **리터럴 고정** — 마이그레이션·모델에 상수로 박는다.
> `EMBEDDING_DIM` env는 (a) 임베딩 응답 차원 검증, (b) OpenAI `dimensions` 파라미터 전달용이며
> **컬럼 차원을 바꾸는 스위치가 아니다**. 기동 시 `EMBEDDING_DIM != 1536`이면 **fail-fast**.

## 1. ERD

```mermaid
erDiagram
    users ||--o{ project_members : ""
    projects ||--o{ project_members : ""
    projects ||--o{ documents : ""
    projects ||--o{ questions : ""
    projects ||--o{ official_qas : ""
    projects ||--o{ lessons : ""
    projects ||--o{ integrations : ""
    projects ||--o{ events : ""
    projects ||--o| guidelines : ""
    projects ||--o{ review_cards : ""
    projects ||--o{ briefing_runs : ""
    documents ||--o{ document_versions : ""
    document_versions ||--o{ chunks : ""
    document_versions ||--o{ review_cards : ""
    questions ||--o| answers : ""
    questions ||--o{ review_cards : ""
    answers ||--o{ answer_citations : ""
    answers ||--o{ feedbacks : ""
    answers ||--o{ review_cards : ""
    chunks ||--o{ answer_citations : ""
    official_qas ||--o{ answer_citations : ""
    users ||--o{ notifications : ""
    users ||--o{ questions : ""
    users ||--o{ feedbacks : ""
```

- `projects ||--o| guidelines` — **0 또는 1**. 신규 프로젝트는 `guidelines` 행이 없으며, 최초 저장 시 생성된다(upsert).
- `questions ||--o| answers` — 질문당 답변 **최대 1행**(`UNIQUE(question_id)`, §7). MVP는 답변 재생성 API를 제공하지 않는다.
- `answers ↔ official_qas` — **순환 FK다.** `answers.official_qa_id`·`answers.similar_official_qa_id`가 `official_qas`를 가리키고, `official_qas.source_answer_id`가 `answers`를 되짚는다. 마이그레이션은 `answers` → `official_qas` 순으로 만든 뒤 앞의 두 FK를 `ALTER`로 붙이고, 모델은 `use_alter=True`를 단다(없으면 `Base.metadata.create_all`이 `CircularDependencyError`로 죽어 테스트가 통째로 멈춘다).
- `review_cards`는 `project_id`(인박스 조회 키) · `question_id`(필수) · `answer_id`(NULL 가능) · `document_version_id`(NULL 가능)를 함께 참조한다.

## 2. 테이블 정의

### users
| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| id | uuid PK | |
| email | text UNIQUE | 로그인 ID |
| password_hash | text | argon2 |
| name | text | |
| language | text | `ko` \| `en` |
| timezone | text | IANA (예: `Asia/Seoul`) |

### projects
| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| id | uuid PK | |
| name / description | text | |
| invite_code | text UNIQUE | 참여용, 재발급 가능 |
| answerer_id | uuid FK users | **담당자 1명** (생성자 기본, 6.3으로 교체) |
| away_mode | bool | 퇴근 모드 (기본 false) |
| settings | jsonb | 아래 §3 설정 스키마 |

### guidelines (프로젝트당 1행)
| project_id | uuid PK FK | |
| content | text | 응답 지침 (기능 1.3) |
| updated_by | uuid FK users | |

### project_members
| id | uuid PK | |
| project_id / user_id | uuid FK | UNIQUE(project_id, user_id) |
| role | text | `answerer` \| `asker` |
| status | text | `active` \| `left` |
| joined_at | timestamptz | |

### documents
| id | uuid PK | |
| project_id | uuid FK | |
| title | text | |
| source_type | text | `upload` \| `notion` \| `github` |
| source_ref | text NULL | notion page_id / github `owner/repo:path` |
| status | text | `active` \| `deleted` (soft delete) |

### document_versions
| id | uuid PK | |
| document_id | uuid FK | |
| version_no | int | 문서 내 1부터 증가 |
| original_filename / mime | text | |
| storage_path | text | `STORAGE_DIR` 상대 경로 |
| is_active | bool | 검색 대상 여부 — 문서당 활성 1개 |
| ingest_status | text | `pending` \| `processing` \| `ready` \| `failed` |
| ingest_error | text NULL | |
| uploaded_by | uuid FK users | |

### chunks
| id | uuid PK | |
| document_version_id | uuid FK | |
| seq | int | 버전 내 순서 |
| content | text | 원문(영어) 그대로 |
| meta | jsonb | `{heading_path, page_no}` |
| embedding | **vector(1536)** | HNSW 인덱스. 차원은 리터럴 고정(문서 상단) |

### questions
| id | uuid PK | |
| project_id / asker_id | uuid FK | |
| content_ko | text | 원문 |
| content_en | text NULL | 번역문 (파이프라인이 채움) |
| urgency | text | `normal` \| `urgent` (질문자 지정) |
| suggest_urgent | bool | AI 제안 플래그 (D10) |
| status | text | `processing` \| `answered` \| `held` \| `failed` |

### answers
| id | uuid PK | |
| question_id | uuid FK | **UNIQUE** — 질문당 최대 1행. MVP는 답변 재생성 API 미제공 (§7) |
| grade | text | `green` \| `yellow` \| `red` |
| matching_rate / search_score / grounding_score | int | 0~100. reused면 NULL 허용 |
| sim_raw | float NULL | **리스케일 전 원시 코사인 유사도(top-1)**. 실측 기록·캘리브레이션 근거 |
| held_reason | text NULL | 🔴 보류 사유 — `conflict` \| `no_evidence` \| `low_confidence` \| `schema_failed` \| `quota_exceeded` |
| question_struct | jsonb NULL | M3에서 저장하는 영어 구조화 `{background, question, options[]}`. M4에서 카드로 복사 |
| state | text | `draft` \| `verified` \| `under_review` \| `expired` \| `rejected` |
| content_ko / content_en | text | 🔴은 발행 안 하지만 내부 초안 보관(카드 표시용) |
| source | text | `generated` \| `reused` |
| official_qa_id | uuid FK NULL | reused 원본 / 확정 시 편입된 Q&A |
| similar_official_qa_id | uuid FK NULL | `similar_threshold`~`reuse_threshold` 구간에서 첨부된 "비슷한 확정 답변" (룰 4·D24). `05 §6` 의 `similar_official_qa` 가 여기서 나온다 |
| degraded_from_red | bool | DND 강등 여부 (룰 6) |
| expires_at | timestamptz NULL | draft 생성 +`draft_expire_hours`(72h). **재사용 답변은 NULL** (D11) |
| verified_at / verified_by | | 확정 정보 |

### answer_citations
| id | uuid PK | |
| answer_id | uuid FK | |
| chunk_id | uuid FK NULL | 문서 근거 |
| official_qa_id | uuid FK NULL | 공식 Q&A 근거 — **D24 이후 미사용. 항상 NULL** (`05 §6` `citations[]` 스키마에 대응 필드가 없다) |
| quote | text | 인용 원문 스니펫 |
| similarity | float | |

### feedbacks (크로스체크)
| id | uuid PK | |
| answer_id / user_id | uuid FK | |
| verdict | text | `correct`(맞았다) \| `different`(달랐다) |
| note | text NULL | 달랐다 사유 한 줄 |
| resolved | bool | 담당자 처리 시 true (룰 9) |

### review_cards (확인 카드 = 담당자 인박스)
| id | uuid PK | |
| project_id / question_id | uuid FK | |
| answer_id | uuid FK NULL | `reason='failed'`이면 NULL (D23) |
| reason | text | `green` \| `yellow` \| `red` \| `feedback` \| `doc_update` \| `failed` |
| document_version_id | uuid FK NULL | `reason='doc_update'`일 때 **새로 활성화된 버전**(재검토를 유발한 쪽). 그 외 NULL |
| status | text | `pending` \| `deferred` \| `resolved` |
| resolution | text NULL | `approved` \| `edited` \| `rejected` \| `kept`(원안 유지/전체 유지) |
| question_struct | jsonb NULL | 영어 구조화 `{background, question, options[]}` (🔴 전달용, 룰 8). `answers.question_struct`에서 복사 |
| recommend_approve | bool | 맞았다 2건↑ 승인 추천 (룰 3) |
| is_urgent | bool | 즉시 알림 대상 여부 |
| first_viewed_at | timestamptz NULL | 카드 처리 시간 지표 시작점 |
| deferred_until / resolved_at | timestamptz | |
| resolved_by | uuid FK users NULL | 처리한 담당자. **`05 §1.4`의 409 `ALREADY_RESOLVED` body가 `resolved_by: {id, name}`를 요구**한다. 담당자는 교체되므로(D16) "현재 담당자"로 대체할 수 없다 (M4에서 추가, 2026-08-08 승인) |

- `reason='green'`: 🟢 답변도 카드를 **큐에 적재**하되 **브리핑·알림 대상에서는 제외**한다. `correct` 피드백 2건 누적으로 `recommend_approve=true`가 될 때만 브리핑 최상단에 올라온다 (룰 3).
- `reason='failed'`: 파이프라인 실패 안전망 카드. `answer_id=NULL`, 초안 없음, 질문자에게는 `answer.failed` 알림 (D23).

### official_qas (공식 Q&A = 확정 지식)
| id | uuid PK | |
| project_id | uuid FK | |
| question_ko / question_en | text | |
| answer_ko | text | **확정 한국어 원문 — 불변** (룰 4·D5) |
| answer_en | text | |
| question_embedding | **vector(1536)** | 재사용 검색용, HNSW. 차원은 리터럴 고정(문서 상단) |
| source_answer_id | uuid FK | |
| status | text | `active` \| `under_review` \| `archived` — under_review 중 재사용 금지 (D7) |
| correct_count | int | 맞았다 누적 |
| reuse_count | int | 재질문 즉답률 원천 |

- **`archived` 전이 규칙**: (1) `DELETE /official-qas/{id}`(담당자 전용) 호출, (2) 원본 문서 soft delete 시 그 문서에서 파생된 Q&A 자동 archived (D20).
  `archived`는 **재사용 검색·유사 첨부 대상에서 완전 제외**되며 MVP에서는 되돌리지 않는다. 전이 시 `official_qa.archived` 이벤트를 기록한다(§5).
- `under_review` 전이 시, 이 Q&A를 `official_qa_id`로 참조하는 `source='reused'` 답변도 함께 `under_review`로 내려간다 (D21).

### lessons (교훈)
| id | uuid PK | |
| project_id | uuid FK | |
| content | text | 한 줄 원칙 |
| content_hash | text | 삭제 후 재생성 차단 (D8) |
| status | text | `candidate` \| `approved` \| `deleted` |
| needs_recheck | bool | 문서 갱신 시 true (룰 5) |
| source_answer_id | uuid FK | |
| last_used_at | timestamptz NULL | 30개 초과 정리 제안 기준 |

### notifications (인앱 알림함)
| id | uuid PK | |
| user_id / project_id | uuid FK | |
| type | text | §4 알림 타입 |
| title / body | text | 수신자 언어로 저장 |
| payload | jsonb | `{question_id, answer_id, card_id...}` 딥링크용 |
| deliver_after | timestamptz NULL | **NULL = 즉시 발송**, 값 = 그 시각(다음 브리핑) 이후 발송 — 비긴급 카드 알림 보류용 (룰 6) |
| read_at | timestamptz NULL | |

### events (이력 — 지표의 단일 원천)
| id | uuid PK | |
| project_id | uuid FK | |
| actor_id | uuid FK NULL | NULL = system |
| type | text | §5 이벤트 타입 |
| entity_type / entity_id | text / uuid | |
| payload | jsonb | §5의 타입별 payload. 등급 산출 이벤트는 `S_raw`·`S`·`G_raw`·`G_final`·`removed_sentences`를 전부 기록 |

### briefing_runs (브리핑 중복 발송 방지)
| id | uuid PK | |
| project_id | uuid FK | |
| run_date | date | 담당자 `users.timezone` 기준 발송 대상 날짜 |
| sent_at | timestamptz | 실제 발송 시각 |

- 단일 프로세스 전제(`--workers 1`)에서도 재시작·수동 트리거로 중복 발송이 가능하다. **락이 아니라 제약으로 막는다**:
  `UNIQUE(project_id, run_date)` + `INSERT … ON CONFLICT DO NOTHING` → `rowcount == 1`일 때만 실제 발송한다 (결정 1.11, `03 §6`).

### integrations
| id | uuid PK | |
| project_id | uuid FK | |
| provider | text | `notion` \| `github` |
| config | jsonb | 토큰은 Fernet 암호화 문자열로 저장 |
| last_synced_at | timestamptz NULL | |
| last_sync_status | text NULL | `ok` \| `failed` 두 값만 (CHECK). **에러 메시지·실패 목록은 여기 두지 않는다** — `sync.run` 이벤트의 payload가 단일 원천이다 (룰 4, **사용자 결정 2026-08-08**). 조회는 `05 §13` `?entity_type=integration&entity_id=…` |

- `last_sync_status`는 **실행 단위 판정**이다. 파일 하나가 실패해도 나머지가 들어왔으면 `ok`이며(실패 격리, `08 §6`), 열거 자체가 실패했거나 시도한 것이 전부 실패했을 때만 `failed`다.
- 마지막으로 본 파일 sha·편집 시각을 담는 **커서 컬럼을 두지 않는다.** GitHub은 저장된 파일에서 blob sha를 다시 계산하고 Notion은 최신 버전의 `created_at`과 비교한다 — 커서를 따로 들면 문서를 지우거나 되돌렸을 때 커서만 앞서 나가 "바뀌었는데 안 가져온다"가 조용히 생긴다.

## 3. projects.settings 스키마 (기본값)

```json
{
  "green_threshold": 80,
  "yellow_threshold": 50,
  "grounding_min": 60,
  "s_floor": 0.25,
  "s_ceil": 0.679,
  "similarity_floor": 0.444,
  "reuse_threshold": 0.925,
  "similar_threshold": 0.855,
  "draft_expire_hours": 72,
  "max_lessons": 30,
  "retrieval_top_k": 6,
  "daily_llm_call_limit": 500,
  "saved_wait_assumption_hours": 24,
  "briefing_hour": 9,
  "dnd_start": "22:00",
  "dnd_end": "07:00"
}
```
- 룰 1의 "코드에 박지 말 것" 대상 전부 포함. PATCH settings로 부분 갱신 (기능 1.4).
- **유사도 리스케일** — `s_floor`/`s_ceil`은 원시 코사인을 0~100 등급 스케일로 옮기는 변환의 양 끝점이다:
  `S = round(clamp((sim_raw - s_floor) / (s_ceil - s_floor), 0, 1) × 100)`.
  `text-embedding-3-small`의 코사인 분포(평균 ≈0.43)를 흡수하므로 `green_threshold`/`yellow_threshold`(80/50)는 그대로 유지된다.
- `similarity_floor`: top-1 `sim_raw`가 이 값 미만이면 근거 없음으로 보고 **강제 🔴 `no_evidence`**. M-1 실측 전까지는 `s_floor` 파생값이었으나, **판정 4에서 독립 값으로 확정됐다** — 무근거 질문(Q9 0.3645)이 🟢 대역 하단(Q5 0.4811)보다 낮아 유사도만으로 분리 가능함이 실측됐고, 두 값의 중간인 **0.423**을 채택했다.
  ⚠️ **2026-08-09 에 0.444 로 상향했다** (사용자 결정, 실측 근거 `03 §4.1.3`). 0.423 은 Q8 의 분포
  **안쪽**이었다 — 같은 질문을 5회 번역해 재보니 Q8 top-1 이 0.4074~0.4258 로 흔들렸고 네 모델 중
  셋이 0.423 을 넘겼다. 넘기면 강제 🔴 이 풀려 **시나리오 B 가 사라진다**. 새 값은 같은 산식으로
  다시 뽑았다 — (🔴 대역 최댓값 0.4258 + 🟢🟡 대역 최솟값 0.4628) / 2 = **0.444**.
  0.423~0.444 구간에 걸린 질문이 없어 다른 등급은 영향받지 않는다.
- `reuse_threshold` / `similar_threshold`는 **리스케일하지 않은 원시 코사인**에 적용한다. 재사용 답변은 `matching_rate: null`이라 화면에 %가 뜨지 않으므로 표기 모순이 없다.
- 위 임계값 5종은 **M-1 게이트 실측 확정값**이다 (**실측 2026-08-07**). 판정 표 원문은 `calibration-2026-08-07.txt`(1차 시도는 `-r1-q3-mismatch.txt`)에 보존돼 있다.
  - `s_ceil` **0.679** — 스크립트 제안(관측 상위 10% 지점) 그대로.
  - `s_floor` **0.25** — 손 조정값이다. 스크립트 자동 제안은 `min(전체 관측) − 0.02`인데 그 최소값이 **강제 🔴 대상인 Q9(0.3645)**여서 0.344가 나왔고, 그 구간에서는 🟢 대역 하단(Q3 43·Q5 41)이 RED로 떨어졌다. 🟢 대역 최소(Q5 0.4811)가 🟡을 유지하는 상한은 `2×0.4811 − 0.679 = 0.283`이며, 여기서 마진 0.03을 뺀 값이다. 결과: 🟢 대역 54~100, 강제 🔴 대상 27~40, 간격 +14.
  - ⚠️ **임계값으로 강제 🔴을 만들려 하지 말 것.** 리스케일은 원시 유사도에 단조이므로 Q3를 올리면 Q8·Q9도 반드시 함께 올라간다. 강제 🔴 4종은 `similarity_floor`와 ④의 `conflict`/`not_answerable` 플래그가 담당한다.
- `daily_llm_call_limit`: 프로젝트 일일 LLM 호출 상한. 초과 시 강제 🔴 `quota_exceeded`. **env가 아닌 settings**에 둔다(룰 1 일관성).
- `saved_wait_assumption_hours`: 절약 대기시간 지표(§5)의 가정치. 화면에 "가정치"로 표기된다.
- **`briefing_timezone`은 두지 않는다.** 브리핑·DND 시각 판정의 단일 원천은 항상 현재 담당자(`projects.answerer_id`)의 `users.timezone`이며, 담당자 교체 시 자동으로 따라간다. `05 §8` 응답의 `timezone`은 여기서 나오는 **파생값**이다.

## 4. 알림 타입

| type | 수신자 | 시점 |
| --- | --- | --- |
| `answer.completed` | 질문자 | 파이프라인 완료 (🟢/🟡 발행, 🔴 보류 안내) |
| `answer.failed` | 질문자 | **파이프라인 실패** — 답변 없이 담당자 카드로 전달되었음을 안내 (D23) |
| `answer.verified` | 질문자 | 승인 확정 |
| `answer.corrected` | 질문자+담당자 | 수정 확정 (양쪽 언어, 룰 8) |
| `answer.kept` | 질문자 | 원안 유지 + 사유 |
| `answer.rejected` | 질문자 | 반려 + 사유 |
| `card.created` | 담당자 | urgent만 즉시, 나머지는 `deliver_after`=다음 브리핑 시각 (룰 6). **`reason='green'` 카드는 알림 대상이 아니다** — 큐에만 적재된다 |
| `briefing.ready` | 담당자 | 브리핑 시각 배치 |
| `doc.review_needed` | 담당자 | 새 버전 활성화, "확정 답변 N건 재검토" (룰 5) |
| `feedback.different` | 담당자 | 달랐다 접수 (urgent 준하여 브리핑 기본) |
| `sync.completed` / `sync.failed` | 담당자 | 연동 동기화 결과 |

## 5. 이벤트 타입 (지표 매핑)

`question.created` `question.status_changed`(payload: from, to) `question.graded`(payload: grade, matching_rate, elapsed_ms) `answer.published` `answer.reused` `answer.reuse_missed`(payload: best_similarity) `answer.expired` `feedback.created`(verdict) `card.created` `card.viewed` `card.approved` `card.edited` `card.rejected` `card.deferred` `card.kept`(payload: bulk) `official_qa.created` `official_qa.suspended` `official_qa.archived` `lesson.candidate` `lesson.approved` `lesson.deleted` `document.version_activated` `answers.review_cascade`(payload: count) `member.joined` `answerer.transferred` `sync.run`

- `answer.reuse_missed`: 재사용 후보를 찾았으나 임계값 미달(또는 LLM 동일성 게이트 `no`)로 재사용하지 않고 새로 생성한 경우. **재질문 즉답률의 분모**를 만든다 (D26).
- `question.status_changed`: §6.1의 모든 전이에서 발행. `held → answered`(카드 확정) 추적의 단일 원천.
- `answer-option` 확정은 `05 §7.2`에 따라 `edit`과 동일 처리이므로 `card.edited`(payload에 `selected_option_index`)로 기록한다.
- `sync.run`: 연동 동기화 1회의 결과. **`entity_type='integration'`** 이며(질문 스코프 규약의 예외 — 한 번의 동기화가 여러 문서에 걸친다) payload는 `{provider, status, scanned, new_documents, new_versions, unchanged, skipped, repaired, restored, failed[]}`다. 열거 자체가 실패하면 `fatal`이 붙는다. **`failed[]`가 실패 파일 목록이 사는 유일한 곳**이다 (`08 §6`, 사용자 결정 2026-08-08).
- **등급 산출 증적**: `question.graded`의 `payload`에는 `S_raw`(= `sim_raw`) · `S`(리스케일 후) · `G_raw`(프루닝 전) · `G_final` · `removed_sentences`(무근거로 제거된 문장)를 **전부 기록**한다. 환각 방어 2겹의 증적이며 캘리브레이션 재산출 근거다.

| 지표 | 계산 |
| --- | --- |
| 자동응답률 | `question.graded`에서 grade in (green, yellow) 비율 |
| 정정률 | `feedback.created` 중 different 비율 |
| 실측 정확도 (등급별) | 등급별 `(verified 또는 correct 피드백 ≥1건) / 해당 등급 전체 발행 수`. **표본 30 미만이면 값 대신 "표본 부족" 표기** (D25) |
| 카드 처리 시간 | `card.viewed` → `card.*` 액션까지 30초 내 비율 |
| 재질문 즉답률 | `answer.reused` / (`answer.reused` + `answer.reuse_missed`) (D26) |
| 절약 대기시간(데모용) | 🟢+🟡 질문 수 × `settings.saved_wait_assumption_hours`(기본 24h, **가정치**) |

## 6. 답변 상태 전이

```mermaid
stateDiagram-v2
    [*] --> draft : 파이프라인 발행 (🟢/🟡) · 🔴 내부 초안
    [*] --> verified : 재사용 즉답 (source=reused, D11)
    draft --> verified : 담당자 승인/수정 (룰 3)
    draft --> expired : 72h 무처리 + 살아 있는 카드 없음 (스위퍼, D14)
    draft --> under_review : 달랐다 피드백
    draft --> rejected : 반려
    verified --> under_review : 달랐다 / 문서 버전 활성화(룰 5) / 참조 공식 Q&A under_review(D21)
    under_review --> verified : 수정 저장 or 원안 유지
    under_review --> rejected : 재검토 후 반려
    note right of verified : 공식 Q&A 편입\nanswer_ko 불변
```
- 🔴 답변은 발행되지 않고 `answers` 행은 draft로 남아 카드에서만 노출. 승인·수정 시 verified로 직행.
- **재사용 답변**(`source='reused'`)은 draft를 거치지 않고 곧바로 `verified`로 진입한다. `expires_at=NULL`, 카드 미생성, 만료 스위퍼 대상 아님 (D11).
- **피드백 허용 상태**는 `draft`·`verified`뿐이다. `expired`·`rejected`·`under_review`에 대한 피드백은 409 `FEEDBACK_NOT_ALLOWED` (D12).
- **`expired`는 종착 상태**다. 만료 답변에 대한 카드 확정 시도는 409로 거부한다 (D13).
  단 스위퍼가 `draft && expires_at < now && 연결된 review_cards 중 pending/deferred 없음`을 모두 만족할 때만 동작하므로(D14),
  **살아 있는 카드 밑의 답변은 만료되지 않는다** — 담당자가 72h 이후에 지연(deferred) 카드를 처리해도 그 답변은 여전히 `draft`이며 `draft → verified`로 확정된다.

## 6.1 질문 상태 전이

```mermaid
stateDiagram-v2
    [*] --> processing : 질문 접수 (202)
    processing --> answered : 🟢/🟡 발행 또는 재사용 즉답
    processing --> held : 🔴 보류 — 확인 카드 생성 (룰 2)
    processing --> failed : 파이프라인 실패 (D23)
    held --> answered : 카드 approve / edit / answer-option 확정
    failed --> answered : 실패 카드를 담당자가 직접 처리
```

| 전이 | 트리거 | 부수 효과 |
| --- | --- | --- |
| `processing → answered` | 🟢/🟡 발행, 또는 재사용 즉답 | `answer.completed` 알림, `question.graded` 이벤트 |
| `processing → held` | 강제 🔴(충돌·근거없음·스키마실패·한도초과) 또는 매칭률 미달 | `answers.held_reason` 기록, `review_cards(reason='red')` 생성 |
| `processing → failed` | 파이프라인 예외·데드라인 초과 후 복구 실패, 좀비 회수 잡(`03 §2`) | `review_cards(reason='failed', answer_id=NULL)` 생성, `answer.failed` 알림 (D23) |
| `held → answered` | 담당자가 카드를 **approve / edit / answer-option** 중 하나로 확정 | `answer` 객체가 채워지고 질문 화면에 답변이 노출된다. `held_info`는 이력용으로 유지하되 `card_status`를 `resolved`로 갱신 (`05 §6`) |
| `failed → answered` | 담당자가 실패 카드에 직접 답변 작성 | **재처리(파이프라인 재실행) API는 MVP 미지원** — 해소 경로는 카드뿐이다 |

- `reject`로 해소되면 `status`는 `held`를 유지하고 `held_info.card_status`만 `resolved`가 된다. `answer.state`는 `rejected`이며 질문자 화면에 답변을 노출하지 않는다 (`05 §6`·`05 §7.1`).
- 모든 전이는 `question.status_changed`(payload: `from`, `to`) 이벤트를 발행한다 (§5).
- `answered`·`failed`에서 되돌아가는 전이는 없다. 답변 재생성 API가 없으므로(`answers`의 `UNIQUE(question_id)`) 질문 하나의 종착은 `answered` 또는 `failed`다.

## 6.2 `llm_usage` — LLM 호출 사용량·비용 (운영 전환, 2026-08-11)

**호출 1회 = 1행.** 종전에는 이 정보가 어디에도 없었다 — 호출 수는 프로세스 메모리
딕셔너리라 재시작하면 0 이 됐고 토큰·비용은 아예 기록되지 않았다.

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `project_id` | uuid NOT NULL | 비용의 소유 주체 |
| `user_id` | uuid NULL | 촉발한 계정. 스케줄러 경유 호출은 NULL |
| `question_id` | uuid NULL | 파이프라인 밖 호출(확정문 번역·인제스트 임베딩)은 NULL |
| `step` | text | `translate` `reuse_gate` `generate` `verify` `structure` `answer_translate` `lesson` `embed` |
| `model` | text | 호출 모델명 |
| `input_tokens` / `output_tokens` | int | |
| `reasoning_tokens` | int | ⚠️ **`output_tokens` 에 포함된 값**이다. 합계에서 따로 더하면 이중 계상 |
| `cost_usd` | numeric(12,6) | **기록 시점 단가로 스냅샷**. 조회 때 재계산하지 않는다 |

- 인덱스: `(project_id, created_at)` — 일일 한도 판정 + 프로젝트 집계 /
  `(user_id, created_at)` — 계정별 집계
- **`events` 가 아닌 이유**: 룰 4 의 대상은 *상태 변화*이지 자원 소비가 아니다. 조회 패턴도
  다르다 — events 는 타임라인(프로젝트+시각), 여기는 집계(주체+기간 SUM).
- **`daily_llm_call_limit` 판정이 이 테이블에서 파생된다** (`services/pipeline/quota.py`).
  ⚠️ 진행 중인 호출은 파이프라인 종료 시 한 번에 적재되므로 아직 세어지지 않는다 —
  상한의 성격이 회계가 아니라 폭주 방어라서 이 오차를 허용한다.

## 6.3 `job_runs` — 예약 작업 실행 기록 (운영 전환, 2026-08-11)

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `job_name` | text | `expiry_sweeper` / `zombie_recovery` / `briefing` |
| `trigger` | text | `interval`(주기) / `startup`(기동 직후 1회) |
| `started_at` / `finished_at` | timestamptz | |
| `status` | text | `ok` / `failed` |
| `processed_count` | int | 실제로 건드린 건수. **0 이 정상인 잡이 대부분**이라 실패와 구분해야 한다 |
| `error` | text NULL | 500자로 자른다 |

- 인덱스: `(job_name, started_at)` — "이 잡이 마지막으로 언제 돌았나"가 유일한 조회 패턴
- ⚠️ **큐가 아니다.** 잡 3종이 전부 상태 파생형이라 할 일은 DB 상태에서 다시 계산된다.
  여기 남기는 것은 할 일이 아니라 **한 일**이다 (`06 §4`).
- ⚠️ **프로젝트 스코프가 아니다** — `project_id` 가 없다. `seed.py --reset` 의 삭제
  대상에서 제외되는 두 테이블 중 하나다(다른 하나는 `users`).

## 7. 인덱스·제약

- `chunks.embedding`, `official_qas.question_embedding`: `USING hnsw (embedding vector_cosine_ops)`
  - 검색은 항상 **`1 - (embedding <=> :q)`** 로 유사도를 만든다. `<=>`는 **거리**이며, SQLAlchemy 헬퍼명이 `cosine_distance()`인 점에 주의(이름만 보고 유사도로 오해하기 쉽다).
  - HNSW post-filtering 대응: 검색 트랜잭션에서 `SET LOCAL hnsw.iterative_scan='relaxed_order'; SET LOCAL hnsw.ef_search=100;` — **pgvector 0.8.0 이상** 필요. 로컬 이미지와 배포 DB 양쪽에서 `SELECT extversion FROM pg_extension WHERE extname='vector'`로 확인한다.
  - 근거 검색(`06 §2` ③)은 `chunks`만 대상으로 한다(D24). `official_qas.question_embedding`은 `06 §2` ②의 재사용·유사 첨부 판정에만 쓰며, **두 인덱스를 하나의 랭킹으로 병합하지 않는다.**
- `answers`: **UNIQUE `(question_id)`** — 질문당 1행. MVP는 답변 재생성 API를 제공하지 않는다(§6.1).
- `document_versions`: 부분 UNIQUE `(document_id) WHERE is_active` — 활성 버전 1개 강제
- `documents`: 부분 UNIQUE `(project_id, source_type, source_ref) WHERE source_ref IS NOT NULL` — 외부 원본 1개 = 문서 1개 (**사용자 결정 2026-08-08**, M8). 업로드 문서는 `source_ref`가 NULL이라 대상 밖이다.
  - 동기화는 "이미 있나?"를 조회한 뒤 INSERT하는데, 담당자가 동기화를 연달아 트리거하면 두 실행이 조회와 INSERT 사이에서 겹쳐 **같은 파일이 문서 두 개**가 된다. 그 뒤로는 한쪽만 갱신되고 다른 쪽이 낡은 내용으로 활성 상태를 유지해 검색에 계속 잡힌다 — 룰 6("근거 문서 내용만")이 낡은 근거를 인용하는 형태로 조용히 깨진다.
  - **락이 아니라 제약으로 막는다** — `briefing_runs`의 중복 발송 방지(결정 1.11)와 같은 방식이다. 동기화는 네트워크 호출을 세션 밖에서 하므로 조회~INSERT 구간에 락을 걸면 원격 응답을 기다리는 동안 커넥션을 붙잡게 된다. 충돌한 쪽은 `IntegrityError`를 받고 **새 문서가 아니라 새 버전 경로로 재시도**한다(내용이 같으면 아무것도 만들지 않는다).
- `review_cards`: `(project_id, status)`, `events`: `(project_id, created_at)`, `notifications`: `(user_id, read_at)`
  - 브리핑 조회는 `notifications(user_id, deliver_after)`도 함께 탄다.
- `feedbacks`: **UNIQUE `(answer_id, user_id)`** — 유저당 1건. 재제출은 409이며 verdict 변경은 지원하지 않는다 (D12).
- `briefing_runs`: **UNIQUE `(project_id, run_date)`** — 브리핑 중복 발송 방지. `INSERT … ON CONFLICT DO NOTHING` 후 `rowcount == 1`일 때만 발송한다 (결정 1.11).
- `project_members`: `(project_id) WHERE role='answerer' AND status='active'` 부분 UNIQUE — 담당자 1명 강제 (룰 9)

> ### ⚠️ `created_at`/`updated_at` 기본값은 `clock_timestamp()`다 — `now()`가 아니다
>
> **M7 실측 (2026-08-08).** Postgres의 `now()`는 `transaction_timestamp()`이라 **한 트랜잭션 안에서 값이 고정**된다.
> 요청 하나가 여러 행을 만들면 그 행들의 `created_at`이 **완전히 동일**해지고, `ORDER BY created_at`이 순서를
> 만들지 못해 결과가 **물리적 행 배치에 따라 달라진다**.
>
> | 케이스 | `now()` | `clock_timestamp()` |
> | --- | --- | --- |
> | 한 트랜잭션에서 INSERT 3건 | 세 행이 **같은 시각** | 문장마다 전진 |
> | `ORDER BY created_at` | 순서 없음(행 배치에 의존) | INSERT 순서 그대로 |
>
> 실제로 깨지는 곳:
> - **§5 이벤트 타임라인** (`05 §13`) — 질문 파이프라인 한 번이 `question.graded` · `question.status_changed` ·
>   `answer.published` · `card.created`를 같은 트랜잭션에서 적재한다. 전부 동률이면 "질문의 전체 여정"이
>   조회마다 다른 순서로 나온다. **지표에도 직접 영향** — 카드 처리 시간(`05 §13` `card_handle_30s_rate`)은
>   `card.viewed` → 액션 이벤트의 간격이다.
> - `review_cards` — `05 §7` 큐의 "오래된 순", `card_status_for_question`의 "그 상태를 만든 카드",
>   `open_card_for_answer`의 "가장 최근 카드"가 전부 `created_at` 하나로 정렬한다.
>
> ⚠️ **`updated_at`도 함께 바꾼다.** 하나만 바꾸면 방금 INSERT한 행에서 `updated_at < created_at`이 되어
> 눈에 보이는 모순이 생긴다. 단조로운 시각이 필요 없는 컬럼도 규칙을 하나로 유지한다.
> 구현은 `app/models/base.py`의 `ROW_TIMESTAMP` 하나이며, `alembic/env.py`가
> `compare_server_default=True`라 모델과 DB의 기본값이 어긋나면 `alembic check`가 잡는다.

> ### ⚠️ 부분 UNIQUE 인덱스 스왑 절차 (필독)
>
> **부분 UNIQUE 인덱스는 `DEFERRABLE`을 지원하지 않는다.** 제약 검사를 커밋 시점으로 미룰 수 없으므로,
> "기존 행을 내리고 새 행을 올리는" 전환은 **한 트랜잭션 안에서 반드시 2문으로, 아래 순서대로** 수행한다.
> 한 문장(단일 `UPDATE … CASE`)으로 스왑하려는 시도는 **금지**한다.
>
> ⚠️ **M-1 실측 (2026-08-07) — 단일 UPDATE는 "항상 실패"하지 않는다. 그래서 더 위험하다.**
> 로컬 pg16.14 · 로컬 pg18.4 · 배포 Railway pg18.4 **세 환경 모두 동일한 결과**였다.
>
> | 케이스 | 방향 | 결과 |
> | --- | --- | --- |
> | A | 활성 `id 1 → 2` (낮은→높은) | **성공** `UPDATE 2` |
> | B | 활성 `id 2 → 1` (높은→낮은) | **`23505` duplicate key** |
> | C | 10행 중 활성 `1 → 7` (낮은→높은) | **성공** `UPDATE 10` |
>
> 성패가 **행 처리 순서에 의존**한다. 기존 활성 행이 새 활성 행보다 먼저 처리되면 통과하고, 나중이면 터진다.
> 즉 개발 중 우연히 A·C 방향만 테스트하면 **초록으로 통과한 뒤 운영에서 간헐 실패**한다 — 항상 실패한다면
> 오히려 안전하다(즉시 발견되므로). 아래 2문 절차는 순서와 무관하게 항상 성공함이 같은 프로브에서 확인됐다.
> 실측 원문: `calibration-2026-08-07-sql.txt`.
>
> **활성 버전 교체** (`document_versions`)
> ```sql
> BEGIN;
> SELECT id FROM documents WHERE id = :doc_id FOR UPDATE;              -- ① 상위 행 잠금
> UPDATE document_versions SET is_active = false WHERE document_id = :doc_id;   -- ② 전부 내린다
> UPDATE document_versions SET is_active = true  WHERE id = :new_version_id;    -- ③ 하나만 올린다
> COMMIT;
> ```
>
> **담당자 교체** (`project_members`, D16)
> ```sql
> BEGIN;
> SELECT id FROM projects WHERE id = :project_id FOR UPDATE;           -- ① 상위 행 잠금
> UPDATE project_members SET role = 'asker'    WHERE project_id = :pid AND role = 'answerer';  -- ② 구담당자 강등
> UPDATE project_members SET role = 'answerer' WHERE project_id = :pid AND user_id = :new_id;  -- ③ 신담당자 승격
> UPDATE projects SET answerer_id = :new_id WHERE id = :pid;           -- ④ 같은 트랜잭션에서 갱신
> COMMIT;
> ```
>
> 동시 요청 경합은 ①의 `SELECT … FOR UPDATE`로 상위 행(`documents` / `projects`)을 먼저 잠가 **직렬화**한다.
> 구담당자는 `asker`로 강등되어 프로젝트에 남는다(D16). 담당자는 교체 없이 떠날 수 없으므로 담당자 부재 상태는 발생하지 않는다(D17).
