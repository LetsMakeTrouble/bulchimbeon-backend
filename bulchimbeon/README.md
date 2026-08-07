# 불침번 백엔드 — AI 프로젝트 생성 킷

> 회의에서 확정된 기획서·비즈니스 룰·PRD·기능명세를 기반으로, **Claude Code가 FastAPI 백엔드를
> 마일스톤 단위로 생성**할 수 있게 만든 문서 + 프롬프트 세트.

## 구성

```
bulchimbeon/
├── README.md          ← 이 파일 (사용법)
├── CLAUDE.md          ← 코딩 에이전트 상시 지침 (레포 루트에 위치해야 함)
├── docs/              ← 설계 문서 9개 (프롬프트가 @참조하는 원천)
│   ├── 00-project-brief.md      제품 정의 · 확정 결정사항 15개
│   ├── 01-functional-spec.md    기능명세 1.1~6.3
│   ├── 02-business-rules.md     ★ 동작 규칙 최우선 기준 (충돌 시 이 문서가 이김)
│   ├── 03-tech-spec.md          스택 · 폴더 구조 · 환경변수 · 실행법
│   ├── 04-data-model.md         ERD · 테이블 · 상태 전이
│   ├── 05-api-contract.md       ★ 프론트 팀 전달용 API 계약서
│   ├── 06-ai-pipeline.md        RAG · 매칭률 · 등급 분기 · 교훈
│   ├── 07-build-plan.md         마일스톤 M-1~M9 · 컷라인
│   └── 08-demo-scenario.md      시드 데이터 · 데모 스크립트 · 검증 질문셋
├── prompts/           ← Claude Code 킥오프 프롬프트 11개 (M-1 + M0~M9와 1:1)
│   └── 00-calibration.md   M-1 캘리브레이션 게이트 🔒 (M0보다 먼저 실행)
└── scripts/           ← probe_calibration.py (M-1 캘리브레이션 프로브)
```

## 사용 순서

1. **새 레포 준비** — 빈 폴더(예: `bulchimbeon-api/`)를 만들고 이 킷의 `CLAUDE.md`, `docs/`, `prompts/`, `scripts/`를 통째로 복사한다.
2. **Claude Code 실행** — 그 폴더에서 `claude`를 실행한다. `CLAUDE.md`는 자동 로드된다.
3. **M-1 캘리브레이션 게이트 (필수 · 15분)** — M0보다 **먼저** 실행한다.
   ```
   @prompts/00-calibration.md 이 프롬프트를 실행해줘
   ```
   ⛔ **절대 컷 불가**(`docs/07-build-plan.md` §컷라인). 여기서 확정한 임계값을 `config.py`의 `DEFAULT_SETTINGS`에 **한 곳에만** 반영한 뒤 M0을 시작한다. 완료 전에는 `00-kickoff.md`를 시작하지 않는다.
4. **프롬프트를 순서대로 실행** — 새 세션(또는 `/clear` 후)에서 한 번에 하나씩:
   ```
   @prompts/00-kickoff.md 이 프롬프트를 실행해줘
   ```
   완료 기준(DoD)이 통과된 것을 확인·커밋한 뒤 다음 프롬프트로 넘어간다.
5. **순서**: `00-calibration(M-1) → 00-kickoff(M0) → 01 → 02 → … → 09`
   (03 · 04가 데모의 심장. 시간 부족 시 컷 순서는 `docs/07-build-plan.md` 참조 — 08 연동부터 포기)
6. **프론트 팀 공유** — M1 완료 시점에 `docs/05-api-contract.md` + Swagger URL(`/docs`)을 1차 전달, M4 완료 시 확정 공지.

## 운용 팁

- **프롬프트 1개 = 세션 1개** 권장. 컨텍스트가 섞이면 품질이 떨어진다.
- DoD의 테스트가 깨진 채 다음 단계로 넘어가지 말 것 — 프롬프트마다 `uv run pytest` 통과가 완료 조건에 들어 있다.
- 중간에 설계를 바꾸고 싶으면 **docs를 먼저 고치고** 해당 프롬프트를 다시 실행한다. (문서가 곧 사양)
- 문서 간 충돌을 발견하면 우선순위: `02-business-rules` > `05-api-contract` > `06-ai-pipeline` > 나머지.
- API 키 필요 시점: **0일차 M-1부터** `OPENAI_API_KEY` — 캘리브레이션 프로브는 실 임베딩 호출이 필수이며 FakeLLM으로 대체 불가(`07` "M-1을 맨 앞에 두는 이유"). M0~M2 구현·테스트는 FakeLLM으로 가능하고, 파이프라인 실호출은 M3부터.

## 이 킷이 반영한 확정 결정 (요약)

Claude Code 타겟 · **FastAPI 백엔드 온리**(프론트 별도 팀) · PostgreSQL+pgvector · OpenAI(+프로바이더 추상화) ·
SSE 실시간 · JWT 인증 · MD/TXT/PDF/DOCX · 한↔영 고정 · Notion+GitHub 동기화 · 로컬 compose+클라우드 데모 ·
가상 시드 데이터 · uv+Python 3.12 · 1주+ 일정. 상세는 `docs/00-project-brief.md` §6.
