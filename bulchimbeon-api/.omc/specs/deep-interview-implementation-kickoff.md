# Deep Interview Spec: bulchimbeon-api 구현 착수 방식

## Metadata
- Interview ID: `di-2026-08-07-bulchimbeon-api-kickoff`
- Rounds: 7 (+ Round 0 토폴로지 게이트)
- Final Ambiguity Score: 19%
- Type: brownfield (설계 문서·프롬프트·프로브 스크립트 존재, 앱 코드 0)
- Generated: 2026-08-07
- Threshold: 0.2
- Threshold Source: `default`
- Initial Context Summarized: no
- Status: **PASSED**

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|---|---|---|---|
| Goal Clarity | 0.80 | 0.35 | 0.280 |
| Constraint Clarity | 0.80 | 0.25 | 0.200 |
| Success Criteria | 0.80 | 0.25 | 0.200 |
| Context Clarity | 0.85 | 0.15 | 0.128 |
| **Total Clarity** | | | **0.808** |
| **Ambiguity** | | | **0.192** |

## Topology

| Component | Status | Description | Coverage |
|---|---|---|---|
| `env-bootstrap` 환경 부트스트랩 | active | uv·docker·pgvector 설치, `OPENAI_API_KEY`, git 레포 수립, 문서의 PowerShell→bash 이관 | AC-1 ~ AC-6 |
| `m1-calibration` M-1 캘리브레이션 게이트 | active | 실 임베딩 프로브 + SQL 프로브 → 임계값 5종 확정 → 문서 7곳 반영 | AC-7 ~ AC-10 |
| `session-ops` 세션 운영 방식 | active | 하이브리드 — 핵심은 수동 단일 세션, 기계적 작업은 위임 + 별도 리뷰 패스 | AC-11 ~ AC-13 |
| `scope-schedule` 스코프·일정 | active | 데드라인 없음 → 컷라인 전면 무효화, M0~M9 전 범위(M8 포함) | AC-14 ~ AC-15 |

보류(deferral) 없음. 4개 컴포넌트 모두 활성.

## Goal

`bulchimbeon-api`의 설계 문서(docs 8종 + prompts 11종)를 **실행 가능한 상태로 만든 뒤, M-1부터 M9까지 전 범위를 하이브리드 세션 운영으로 구현한다.**

구체적으로:
1. Linux 실행 환경을 문서의 전제와 일치시킨다 (문서를 환경에 맞게 고치는 방향).
2. `prompts/00-calibration.md`(M-1)를 실측 실행해 임계값 5종을 확정하고 문서 7곳에 반영한다.
3. M0~M9를 순차 진행하되, **컷 불가 핵심(M-1·M3·M4)은 수동 단일 세션**으로, **기계적 마일스톤(M0·M2·M7·M9)은 에이전트 위임**으로 처리하고, 위임분은 반드시 **작성과 분리된 리뷰 패스**를 거친다.

## Constraints

- **실행 환경은 Linux (bash)**. 문서 5개 파일 17곳의 PowerShell 전용 구문은 bash로 **전면 이관**한다 — 문서가 곧 사양이므로(`START-HERE.md:59`) 사양을 환경에 맞춘다.
- **`sudo`는 암호를 요구한다.** 에이전트가 docker를 설치할 수 없다. docker 설치는 사용자가 직접 수행한다.
- **`uv`·`docker`·`psql` 미설치, `pip` 없음, `OPENAI_API_KEY` 미설정.** Python 3.12.3 · venv · apt · curl · git 2.43.0은 있다.
- **git 레포가 아니다.** `/home/kancth03/bulchimbeon` 상위 디렉토리를 **하나의 레포**로 초기화한다 (원본 킷 `bulchimbeon/` + 구현 레포 `bulchimbeon-api/`를 함께 추적).
- **데드라인 없음.** `07-build-plan.md`의 7일 배치와 컷라인 1~6번은 **적용하지 않는다**.
- `OPENAI_API_KEY`는 확보 가능하며 M-1은 실 임베딩 호출로 수행한다 (FakeLLM 대체 불가 — `START-HERE.md:12`).
- 리소스: 11Gi RAM / 177G 디스크 / 2 vCPU — pgvector 컨테이너에 충분.
- 문서 우선순위는 유지: `02-business-rules` > `05-api-contract` > `06-ai-pipeline` > 나머지.
- `CLAUDE.md`의 아키텍처 규칙 9개는 위반 불가 (특히 임계값 하드코딩 금지, LLM 프로바이더 경유, `--workers 1`).

## Non-Goals

- **프론트엔드 구현** — 별도 팀. `docs/05-api-contract.md`가 전달물이다.
- **컷라인 적용** — 데드라인이 없으므로 `07:35-40`의 컷 1~6번은 실행하지 않는다. M8 실구현, 교훈 추출·승인 루프, 알림 다국어 문안, 브리핑 스케줄러 + `deliver_after` 보류 로직, PDF/DOCX 파싱, 지표 4종이 **전부 범위 안**이다.
- **실 API를 호출하는 테스트** — `CLAUDE.md:34` 규칙 유지. 예외는 M-1 프로브와 M3의 실LLM 스모크 1회뿐.
- **`probe_calibration.py`의 상수 수정** — 스크립트는 측정 도구이지 설정 원천이 아니다(`00-calibration.md:93-94`). 조정은 문서 쪽에만.
- **원본 킷 `../bulchimbeon`의 구조 변경** — 참조용으로 보존(`START-HERE.md:3`). 단 상위 레포에 함께 커밋된다.

## Acceptance Criteria

### 환경 부트스트랩 (`env-bootstrap`)
- [ ] **AC-1** `/home/kancth03/bulchimbeon`에서 `git rev-parse --show-toplevel`이 해당 경로를 반환한다. `.gitignore`가 `.omc/`, `__pycache__/`, `*.pyc`, `.env`, `.venv/`를 제외한다.
- [ ] **AC-2** 초기 커밋이 존재하고, 원본 킷과 구현 레포 문서가 모두 추적된다.
- [ ] **AC-3** `uv --version`이 동작한다 (sudo 불필요 — `curl -LsSf https://astral.sh/uv/install.sh | sh`).
- [ ] **AC-4** `docker compose version`이 동작하고 사용자가 docker 그룹에 속하거나 sudo 없이 컨테이너를 띄울 수 있다. *(사용자 직접 수행)*
- [ ] **AC-5** `OPENAI_API_KEY`가 셸 세션에 로드된다. 키는 git에 커밋되지 않는다.
- [ ] **AC-6** 문서 5개 파일 17곳의 PowerShell 구문이 bash로 교체됐다: `START-HERE.md`(2), `CLAUDE.md`(3, 특히 `:19-29`의 `if ($?)`와 `:29`의 PowerShell 경고 문단), `docs/03-tech-spec.md`(5), `prompts/00-kickoff.md`(2), `prompts/00-calibration.md`(4, `chcp 65001` 포함). `grep -rE '\$env:|chcp|if \(\$\?\)' --include='*.md' .` 결과가 0건.

### M-1 캘리브레이션 게이트 (`m1-calibration`)
- [ ] **AC-7** `prompts/00-calibration.md`의 완료 기준 체크리스트 7항목이 전부 충족된다 (원문 그대로 준수).
- [ ] **AC-8** 판정 1의 "제안값 적용 후 등급"에서 🟢 대역(Q1~Q6·Q11~Q13)이 전부 🟢/🟡이 되는 `s_floor`/`s_ceil` 조합을 찾았다. **Q7~Q9를 S 기준으로 🔴에 밀어넣는 임계값 조정은 하지 않았다**(`00-calibration.md:37` — 수학적으로 불가능하며 🟢 대역이 함께 무너진다).
- [ ] **AC-9** `04 §3` · `05 §3` · `08 §1` 설정값 표가 실측값으로 갱신되고 ⚠️ 잠정값 표기가 사라졌다(04와 05가 1:1 일치). 표 **밖**의 인용 기본값 `02:30` · `02:95` · `02:98` · `02:101` · `06:63` · `06:96` · `06:109`도 전수 정정됐다.
- [ ] **AC-10** 판정 표 원문이 `docs/` **밖**의 파일(예: `calibration-2026-08-07.txt`)로 보존됐다. `SELECT extversion` 결과가 로컬 + 배포 DB 양쪽에서 기록됐다.

### 세션 운영 (`session-ops`)
- [ ] **AC-11** 각 마일스톤이 자신의 DoD 테스트를 통과한 상태로 `feat(M{n}): ...` 커밋이 남는다. 테스트가 깨진 채 다음 마일스톤으로 넘어가지 않는다.
- [ ] **AC-12** **위임한 마일스톤**은 DoD 테스트 통과에 더해, **작성과 분리된 별도 리뷰 패스**(`code-reviewer` 또는 `verifier`)가 diff를 읽고 문서 정합성을 확인한 뒤에만 합격 처리된다. 같은 컨텍스트에서 자체 승인하지 않는다.
- [ ] **AC-13** 위임/수동 경계가 지켜진다 — **수동 단일 세션**: M-1, M3, M4. **위임 가능**: M0, M2, M7, M9. **병렬 위임 후보**: M5+M6 (`07:64`가 지목한 병목), M8.

### 스코프 (`scope-schedule`)
- [ ] **AC-14** `docs/05-api-contract.md`의 전 엔드포인트(55개 이상)와 `docs/04-data-model.md §2`의 테이블 18개가 구현된다. M8 `POST /integrations`가 501 스텁이 아닌 실구현이다.
- [ ] **AC-15** 클라우드 URL에서 `08-demo-scenario.md`의 시나리오 A·B가 재현된다 (M9 DoD).

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|---|---|---|
| 문서의 PowerShell 명령이 실행 가능하다 | 실측: bash/Linux 환경, PowerShell 구문 17곳 / bash 구문 0곳 | 문서를 bash로 **전면 이관**. 사양을 환경에 맞춘다 |
| 에이전트가 필요한 도구를 설치할 수 있다 | 실측: `sudo -n` 실패 — 암호 필요 | docker 설치는 **사용자가 직접**. uv는 sudo 없이 에이전트가 설치 가능 |
| `07-build-plan.md`의 7일 일정과 컷라인이 실행 계획이다 | Round 3: 데드라인 없음 | 컷라인 1~6번 **전면 무효화**. 전 범위 실행 |
| 데드라인이 없으면 전 범위가 자동으로 정답이다 | Round 6 Simplifier: 컷라인 일부는 시간이 아니라 **가치 판단**이었다 (M8 "확장 포인트로 발표", 교훈 메모리 "데모 미등장") | 사용자가 전 범위를 명시 선택. M8 실구현 확정 |
| 위임하면 빠르니까 하이브리드가 맞다 | Round 4 Contrarian: 데드라인이 없으면 속도 이득이 소멸. 계획 수립 이득도 이미 `prompts/`에 내장됨 | 위임 근거를 **컨텍스트 오염 방지 + 작성/리뷰 레인 분리**로 재정립. 합격 기준에 별도 리뷰 패스 추가 |
| 마일스톤 커밋은 관행일 뿐이다 | Round 7: git 레포가 아님. 그런데 Round 4의 "리뷰 패스"는 diff에 의존하고 `07:69`는 커밋을 진행 규칙으로 요구 | git이 **합격 판정의 인프라**임이 드러남. 상위 디렉토리를 단일 레포로 초기화 |

## Technical Context

**실측된 환경 (2026-08-07)**

| 항목 | 상태 |
|---|---|
| OS / 셸 | Linux 6.17 (Oracle) / bash |
| Python | 3.12.3 ✅ (문서 요구와 일치) |
| `uv` / `docker` / `psql` / `pip` | 전부 미설치 |
| `venv` / `apt` / `curl` / `git` | 있음 (git 2.43.0) |
| `sudo` | 암호 필요 (무암호 불가) |
| `OPENAI_API_KEY` | 미설정 — 사용자가 확보 가능 |
| git 레포 | 아님 → 상위 디렉토리를 레포로 초기화 예정 |
| 리소스 | 11Gi RAM / 6.5Gi available, 177G 디스크, 2 vCPU |

**기존 자산**

- `docs/` 8종 (총 2,300여 줄) — `05-api-contract.md` 797줄이 최대
- `prompts/` 11종 — 마일스톤별 킥오프 프롬프트, DoD 체크리스트 내장
- `scripts/probe_calibration.py` — M-1 프로브, 실행 가능 상태
- 앱 코드: **0** (M0 스캐폴딩부터 시작)

**부트스트랩 실행 순서 (의존 관계 기준)**

1. `git init` (상위 디렉토리) + `.gitignore` + 초기 커밋 — 이후 모든 변경이 diff로 추적됨
2. `uv` 설치 (에이전트 가능, sudo 불필요)
3. docker 설치 + pgvector 이미지 (**사용자 직접**, sudo 필요)
4. `OPENAI_API_KEY` 설정
5. 문서 17곳 PowerShell → bash 이관 + 커밋
6. **M-1 게이트 실행** ← 여기부터가 `prompts/` 시퀀스의 시작

1·2·5는 3·4와 독립이므로 사용자의 docker 설치를 기다리는 동안 병렬 진행 가능하다.

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|---|---|---|---|
| 마일스톤 | core domain | id(M-1~M9), 이름, 프롬프트 경로, 산출물, DoD, 컷 가능 여부 | 프롬프트-세션 1개와 1:1, DoD 게이트 1개를 가진다 |
| 프롬프트-세션 | core domain | 프롬프트 파일, 실행 모드(수동/위임), 커밋 메시지 | 마일스톤 1개에 속한다 |
| DoD 게이트 | core domain | 체크리스트, 테스트 통과, 리뷰 패스 결과 | 마일스톤의 통과 조건; 위임 시 리뷰 패스가 추가된다 |
| 설계문서 | core domain | 경로, 우선순위 등급 | 사양의 원천; 임계값 세트를 담는다 |
| 임계값 세트 | supporting | `s_floor`, `s_ceil`, `similarity_floor`, `reuse_threshold`, `similar_threshold` | M-1이 확정; `config.py`의 `DEFAULT_SETTINGS` 한 곳에만 반영 |
| 실행환경 | supporting | OS/셸, uv, docker, postgres+pgvector, `OPENAI_API_KEY`, git | 부트스트랩 대상; 마일스톤 실행의 전제 |
| 위임 단위 | supporting | 대상 마일스톤, 실행 에이전트, 리뷰 패스 에이전트 | 마일스톤을 위임받아 DoD 게이트로 판정된다 |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|---|---|---|---|---|---|
| 1 | 6 | 6 | - | - | N/A |
| 2 | 7 | 1 (`위임 단위`) | 0 | 6 | 86% |
| 3 | 7 | 0 | 0 | 7 | 100% |
| 4 | 7 | 0 | 0 | 7 | 100% |
| 5 | 7 | 0 | 0 | 7 | 100% |
| 6 | 7 | 0 | 0 | 7 | 100% |
| 7 | 7 | 0 | 0 | 7 | 100% |

6라운드 연속 무변화 — 도메인 모델 완전 수렴.

## Interview Transcript

<details>
<summary>Full Q&A (Round 0 + 7 rounds)</summary>

### Round 0 — 토폴로지 확인
**Q:** 4개 top-level 컴포넌트(환경 부트스트랩 / M-1 게이트 / 세션 운영 방식 / 스코프·일정)가 맞나?
**A:** 4개 그대로 진행
**결과:** 토폴로지 잠금, 보류 없음

### Round 1 — 환경 부트스트랩 / Constraint Clarity
**Q:** M-1 실행에 필요한 실 OpenAI 키 + pgvector Postgres를 지금 확보할 수 있나? (근거: `sudo -n` 실패로 에이전트가 docker 설치 불가)
**A:** 둘 다 확보 가능
**Ambiguity:** 73% (Goal 0.20, Constraints 0.20, Criteria 0.20, Context 0.70)

### Round 2 — 세션 운영 방식 / Goal Clarity
**Q:** M0~M9를 어떤 운영 방식으로 굴릴 것인가? (`START-HERE.md:4`는 이미 "프롬프트 1개 = 세션 1개"를 지시)
**A:** 하이브리드 — 컷불가 핵심은 수동, 기계적 작업은 위임
**Ambiguity:** 66% (Goal 0.30, Constraints 0.20, Criteria 0.30, Context 0.75)

### Round 3 — 스코프·일정 / Constraint Clarity
**Q:** 실제 데드라인과 하루 투입 시간은? (`07:62-64`의 "하루 14~22시간" 가정이 유효한가)
**A:** 데드라인 없음
**Ambiguity:** 44% (Goal 0.65, Constraints 0.55, Criteria 0.35, Context 0.80)
**파급:** 컷라인 1~6번 전면 무효화

### Round 4 — 세션 운영 방식 / Success Criteria 🔥 Contrarian
**Q:** 데드라인이 없으면 위임의 속도 이득이 사라지고, 계획 이득도 `prompts/`에 이미 내장돼 있다. 그래도 하이브리드를 유지한다면 위임 결과물을 무엇으로 합격 판정하나?
**A:** DoD + 별도 리뷰 패스
**Ambiguity:** 39% (Goal 0.65, Constraints 0.75, Criteria 0.50, Context 0.80)
**파급:** 위임 근거가 "속도"에서 "컨텍스트 오염 방지 + 레인 분리"로 재정립

### Round 5 — 환경 부트스트랩 / Constraint Clarity
**Q:** 문서 5개 파일 17곳의 PowerShell 전용 명령을 어떻게 처리하나? (bash 구문은 0곳)
**A:** bash로 전면 이관
**Ambiguity:** 33% (Goal 0.80, Constraints 0.75, Criteria 0.55, Context 0.80)

### Round 6 — 스코프·일정 / Goal Clarity ✂️ Simplifier
**Q:** 컷라인 일부는 시간이 아니라 가치 판단이었다(M8 "확장 포인트", 교훈 메모리 "데모 미등장"). 가장 단순하면서도 가치 있는 버전은 어디까지인가?
**A:** M0~M9 전 범위 (M8 포함)
**Ambiguity:** 28% (Goal 0.80, Constraints 0.75, Criteria 0.55, Context 0.80)

### Round 7 — 환경 부트스트랩 / Success Criteria
**Q:** git 레포를 어떻게 세우나? (Round 4의 리뷰 패스와 `07:69` 마일스톤 커밋이 둘 다 git에 의존하는데 현재 레포가 아님)
**A:** 상위 디렉토리 `/home/kancth03/bulchimbeon`를 단일 레포로
**Ambiguity:** **19%** ✅ (Goal 0.80, Constraints 0.80, Criteria 0.80, Context 0.85)

</details>

---
**Status: pending approval** — 실행 경로가 명시적으로 승인되기 전까지 어떤 파일도 변경하지 않는다.
