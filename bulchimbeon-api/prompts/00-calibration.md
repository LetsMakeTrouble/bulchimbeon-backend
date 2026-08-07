# M-1 — 캘리브레이션 게이트 (M0보다 먼저 실행 ⛔)

@docs/04-data-model.md §3, @docs/08-demo-scenario.md §2·§3, @docs/03-tech-spec.md §5.4·§6 를 읽고
`scripts/probe_calibration.py`를 실행해 **임계값 5종을 실측으로 확정**해줘.

> 이 게이트를 건너뛰고 M0을 시작하면, 잘못된 임계값 위에 파이프라인 테스트를 작성하게 된다.
> `04 §3`의 `s_floor`·`s_ceil`·`reuse_threshold`·`similar_threshold`는 **⚠️ 캘리브레이션 전 잠정값**으로
> 표기되어 있다. `similarity_floor`는 `s_floor`와 같은 값을 따르는 파생 기본값이라 표기가 없지만
> 이번 게이트에서 함께 갱신한다. 그 표기를 지우는 것이 이 마일스톤의 목적이다.
> 소요 15분. 백엔드 코드 불필요 — OpenAI 키 1개와 Postgres 컨테이너 1개만 있으면 된다.

## 작업

### 1. 임베딩·LLM 프로브 실행

```bash
set -a; . ./.env; set +a          # OPENAI_API_KEY 로드 (.env가 키의 단일 원천)
uv run --with openai python scripts/probe_calibration.py
```

- 아직 `pyproject.toml`이 없으므로 `--with openai`가 필요하다. M0 이후에는 생략 가능.
- 스크립트 상단 독스트링의 **사전 등록 판정 기준표를 먼저 읽는다.** 결과를 보고 기준을 바꾸지 않는다.

### 2. 결과 해석 — 판정 4종

| 판정 | 확정하는 것 | 읽는 법 |
| --- | --- | --- |
| 1 | `s_floor` / `s_ceil` | "제안값 적용 후 등급" 표에서 **🟢 대역(Q1~Q6·Q11~Q13)이 🟢/🟡이면 채택**. ⚠️ Q7~Q9의 S가 높게 나오는 것은 **정상**이며 판정 대상이 아니다 — 강제 🔴은 S가 아니라 ④의 `conflict`/`not_answerable` 플래그가 잡는다(`06 §2` ④, 판정 4) |
| 2 | `reuse_threshold` / `similar_threshold` | Q8↔Q10 실측치 - 0.03. **무관 질문쌍 최대값보다 낮으면 경고가 뜬다** |
| 3 | `LLM_REASONING_EFFORT` 지원·지연 | 400이면 미지원. minimal×3이 25초를 넘으면 데드라인 재설계 |
| 4 | `similarity_floor` | Q9 top-1이 🟢 대역 아래면 채택, 겹치면 ④ 프롬프트 강화에 의존 |

- **MISMATCH가 남으면**(🟢 대역에서만 발생한다) `s_floor`/`s_ceil`을 손으로 조정해 재실행한다. 임베딩은 캐시되지 않으므로 재실행 비용은 매번 <$0.01이다.
- 조정해도 Q1~Q6·Q11~Q13을 🟢/🟡로 올리는 구간이 없으면 **시드 코퍼스 문제**다.
  `08 §2` 문서 본문을 보강하는 쪽이 임계값을 비트는 것보다 옳다(결정 1.7).
- ⛔ **Q7~Q9를 S 기준으로 🔴에 밀어 넣으려고 임계값을 조정하지 마라.** Q7의 top-1은 api-spec `Rate Limiting` 청크와 리터럴 일치가 강해 S가 높게 나오고, 리스케일은 원시 유사도에 **단조**이므로 Q7만 S<50으로 내리는 것은 수학적으로 불가능하다 — 시도하면 🟢 대역이 함께 무너진다. 강제 🔴 4종은 등급 임계값이 아니라 ④의 플래그·`similarity_floor`가 담당한다.
- 판정 3이 400이면 `03 §4`의 `LLM_REASONING_EFFORT`·`LLM_TIMEOUT_SECONDS`·`LLM_PIPELINE_DEADLINE_SECONDS`
  전제가 깨진 것이다. 값을 고치기 전에 **왜 깨졌는지(모델 미지원 / SDK 버전 / 호출 방식)** 를 먼저 기록한다.

### 3. 보조 SQL 프로브

```bash
docker run --rm -d --name pgprobe -e POSTGRES_PASSWORD=probe -p 5433:5432 pgvector/pgvector:pg16
docker exec -i pgprobe psql -U postgres -f -
```

```sql
CREATE EXTENSION IF NOT EXISTS vector;
SELECT extversion FROM pg_extension WHERE extname='vector';   -- 0.8.0 이상? (hnsw.iterative_scan 전제)

SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector AS same,        -- 기대 0 — <=> 는 "거리"다
       '[1,0,0]'::vector <=> '[0,1,0]'::vector AS orthogonal;  -- 기대 1

CREATE TABLE t(id int primary key, doc int, act bool);
CREATE UNIQUE INDEX ON t(doc) WHERE act;
INSERT INTO t VALUES (1,1,true),(2,1,false);
UPDATE t SET act = (id = 2) WHERE doc = 1;   -- 23505 나면 단일 UPDATE 스왑 금지 확정
DROP TABLE t;
```

- **세 프로브가 각각 확정하는 것**
  1. `extversion` < 0.8.0 → `hnsw.iterative_scan`을 쓸 수 없다. 이미지 교체 또는 post-filtering 대안이 필요하다 (`04 §7`).
  2. `same=0`·`orthogonal=1`이면 `<=>`가 거리임이 실증된다. 모든 검색 SQL은 `1 - (embedding <=> :q)`로 유사도를 만든다.
  3. `UPDATE`가 `23505`로 죽으면 부분 UNIQUE 단일 UPDATE 스왑 금지가 실증된다 (`04 §7` 스왑 절차).
- **배포 DB에서도 1번을 확인한다.** Railway/Render 매니지드 Postgres가 `CREATE EXTENSION vector` 권한을 주는지,
  주더라도 버전이 0.8.0 이상인지 — 배포 당일(6일차)이 아니라 **지금** 확인해야 복구 시간이 있다 (`03 §6`).
- 확인 후 `docker rm -f pgprobe`로 정리한다.

### 4. 문서 반영

1. **`docs/04-data-model.md §3`** — `s_floor`·`s_ceil`·`similarity_floor`·`reuse_threshold`·`similar_threshold`
   기본값을 실측값으로 교체하고, **⚠️ 표기가 있는 항목(4종)은 표기를 제거하고 `similarity_floor`는 값만 갱신한다.**
   그 뒤 `실측 YYYY-MM-DD` 를 남긴다.
   판정 3에서 `reasoning_effort`가 미지원이면 `03 §4`의 관련 env도 함께 정정한다.
2. **`docs/05-api-contract.md §3`** settings 허용 키 표의 기본값 열을 동일하게 맞춘다 (04와 1:1이어야 한다).
3. **`docs/08-demo-scenario.md §1` 설정값 표**의 `s_floor`/`s_ceil`·`similarity_floor` 값을 실측값으로 갱신하고
   ⚠️ 잠정값 표기를 제거한다. (⚠️ 표기가 있는 곳은 `08 §3`이 아니라 `08 §1`이다.)
4. **`docs/08-demo-scenario.md §3`** 기대 등급표를 판정 1의 "제안값 적용 후 등급"과 **대조**한다 — 갱신 대상이 아니라
   대조 대상이다(임계값이 실리지 않은 표다).
   - 어긋나는 질문이 있으면 **기대값이 아니라 원인**을 먼저 본다. 임계값으로 맞출 수 있으면 임계값을,
     근거 부족이면 `08 §2` 시드 문서를 보강한다. 불일치는 원인과 함께 기록한다.
   - Q9(무근거)·Q7(충돌)은 유사도가 아니라 ④의 `not_answerable`·`conflict` 플래그로 잡히는 것이 정상이다.
     판정 1에서 이 둘이 🟡로 나오더라도 기대표를 바꾸지 말고 **④ 프롬프트 규칙에 의존한다는 사실을 기록**한다.
5. **인용 기본값 정정** — 설정값 표 **밖**에서 임계값을 숫자로 인용한 곳도 전부 실측값으로 함께 고친다:
   `02:30`(s_floor/s_ceil) · **`02:95`(reuse_threshold)** · **`02:98`(similar_threshold)** · `02:101`(⚠️ 주석) ·
   `06:63`(reuse/similar) · `06:96`(s_floor/s_ceil) · **`06:109`(similarity_floor)**.
   (⚠️ 표기가 없는 `02:95`·`02:98`·`06:109`는 **값만** 갱신한다 — ⚠️를 새로 붙이지 않는다.)
   `02`가 최우선 문서이므로 여기가 stale이면 04·05를 고쳐도 우선순위 규칙상 옛 값이 이긴다.
   특히 `02:101`은 `:95`·`:98` **아래 붙은 주석**이다 — 주석만 고치고 위 규범 문장의 옛 숫자를 남기면 최악의 조합이 된다.
6. **판정 1의 출력 표를 그대로 보존한다** — `docs/` 밖(예: `calibration-2026-08-06.txt`)에 저장.
   IR Deck의 "우리는 임계값을 실측으로 캘리브레이션했다" 슬라이드 자료이자, M9 리허설 때의 비교 기준선이다.
7. 조정한 `s_floor`/`s_ceil`이 스크립트 기본 제안과 다르면 `scripts/probe_calibration.py`의 상수가 아니라
   **문서 쪽만** 고친다. 스크립트는 측정 도구이지 설정 원천이 아니다.

## 완료 기준

- [ ] `probe_calibration.py`가 끝까지 실행되고 판정 1·2·4의 표가 출력됐다 (판정 3은 400이어도 무방 — 결과 기록이 목적)
- [ ] 판정 1의 "제안값 적용 후 등급"에서 **🟢 대역(Q1~Q6·Q11~Q13)이 전부 🟢 또는 🟡**이 되는 `s_floor`/`s_ceil` 조합을 찾았다 (Q7~Q9의 S는 판정 대상이 아니다 — ④ 플래그·`similarity_floor` 소관)
- [ ] `reuse_threshold` 실측값이 정해졌고, 무관 질문쌍 최대값보다 높거나 — 낮다면 **LLM 동일성 2차 게이트가 컷 불가 항목임을 기록**했다
- [ ] `SELECT extversion` 결과(로컬 + 배포 DB 양쪽)를 기록했다. 0.8.0 미만이면 대응 방침을 정했다
- [ ] `<=>`가 거리임을 SQL로 확인했다 (`same=0`, `orthogonal=1`)
- [ ] 부분 UNIQUE 단일 UPDATE가 `23505`로 실패함을 재현했다
- [ ] `04 §3` · `05 §3` · `08 §1` 설정값 표가 실측값으로 갱신되고 ⚠️ 잠정값 표기가 사라졌다(04와 05가 1:1로 일치). `02:30` · `02:95` · `02:98` · `02:101` · `06:63` · `06:96` · `06:109`의 인용 기본값도 함께 정정했다(설정값 표 밖의 숫자 인용을 전수 확인)
- [ ] `08 §3` 기대 등급표를 판정 1의 "제안값 적용 후 등급"과 **대조**하고, 불일치가 있으면 원인(임계값 / 시드 근거 부족)을 기록했다
- [ ] 판정 표 원문이 파일로 보존됐다

> 이 체크리스트가 전부 채워지기 전에는 `prompts/00-kickoff.md`(M0)를 시작하지 않는다.
> M-1은 **절대 컷 불가** 항목이다 (결정 1.18).
