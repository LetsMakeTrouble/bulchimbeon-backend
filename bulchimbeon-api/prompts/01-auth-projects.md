# M1 — 인증 · 프로젝트 · 멤버

@docs/05-api-contract.md §2~§3, @docs/04-data-model.md (users/projects/project_members/guidelines) §7, @docs/02-business-rules.md §9 를 기준으로 인증과 프로젝트 도메인을 구현해줘.

## 작업

1. **모델·마이그레이션**: users, projects, project_members, guidelines — 데이터 모델 문서 그대로.
   담당자 1명 부분 UNIQUE 제약 `(project_id) WHERE role='answerer' AND status='active'` 포함
2. **보안 코어**: `core/security.py` — argon2 해시, JWT 발급/검증(access 30분·refresh 14일, `sub`=user_id).
   `core/deps.py` — `get_current_user`, `require_member(project_id)`, `require_answerer(project_id)`,
   **`require_asker(project_id)`**(담당자 질문 금지 D17을 M3에서 바로 쓸 수 있게 지금 만든다)
3. **인증 API**: signup / login / refresh / me — 계약서 §2의 요청·응답 그대로
4. **프로젝트 API** (계약서 §3 전체):
   - 생성(생성자=담당자, settings는 `DEFAULT_SETTINGS` 복사, invite_code 자동 발급)
   - 목록·상세(내 역할 포함), away-mode PATCH
   - **settings PATCH — 화이트리스트는 `04 §3`의 16개 키 전부**(`05 §3`의 허용 키 표와 1:1).
     타입·범위까지 검증하고 목록 밖 키는 400 `VALIDATION_ERROR`.
     **`briefing_timezone`은 허용 키가 아니다** — PATCH하면 400이다. 브리핑·DND 시각 판정의 단일 원천은
     현재 담당자의 `users.timezone`이며 프로젝트 설정으로 덮어쓸 수 없다.
   - invite-code 재발급, join(중복 참여 409 `INVITE_ALREADY_JOINED`)
   - members 목록, 멤버 제거(asker만 가능)
   - **`POST /projects/{id}/leave`** — 질문자 자진 탈퇴 (D18). `project_members.status='left'`로만 바꾸고
     기존 질문·답변·피드백은 **보존**한다. **담당자가 호출하면 403 `FORBIDDEN_ROLE`** — 담당자는 교체 없이
     떠날 수 없으므로 담당자 부재 상태는 발생하지 않는다 (D17). `left` 멤버의 프로젝트는 `GET /projects` 목록에서 빠진다.
   - guidelines GET(멤버)/PUT(담당자)
5. **`POST /projects/{id}/transfer-answerer` — 트랜잭션 순서를 지킨다 (D16, `04 §7`)**

   부분 UNIQUE 인덱스는 **`DEFERRABLE`을 지원하지 않는다.** 단일 `UPDATE … CASE`로 역할을 스왑하면
   문장 중간 상태에서 제약을 위반해 `23505`로 죽는다. **한 트랜잭션 안에서 아래 4문을 순서대로** 실행한다.

   ```sql
   BEGIN;
   SELECT id FROM projects WHERE id = :pid FOR UPDATE;                                          -- ① 상위 행 잠금(경합 직렬화)
   UPDATE project_members SET role='asker'    WHERE project_id=:pid AND role='answerer';        -- ② 구담당자 강등
   UPDATE project_members SET role='answerer' WHERE project_id=:pid AND user_id=:new_id;        -- ③ 신담당자 승격
   UPDATE projects SET answerer_id=:new_id WHERE id=:pid;                                       -- ④ 같은 트랜잭션에서 갱신
   COMMIT;
   ```

   - **구담당자는 삭제되지 않고 `asker`로 프로젝트에 남는다.**
   - 신담당자가 `status='left'`이거나 멤버가 아니면 400으로 거부한다.
   - 미처리 카드·브리핑 이관은 M4에서 해소한다 — 여기서는 `# TODO(M4): pending/deferred 카드 이관` 주석만 남긴다.
   - 교체 즉시 브리핑·DND 판정 기준 타임존이 신담당자의 `users.timezone`으로 **자동으로 따라간다**(별도 코드 불필요).
6. **이벤트 기록**: event_service 최소 구현 — `member.joined`, `answerer.transferred` 기록 (events 테이블 생성)

## 완료 기준

- 테스트: 가입→로그인→프로젝트 생성→초대 코드로 두 번째 유저 참여→역할 확인 e2e
- 권한 테스트: asker가 settings PATCH 시 403 `FORBIDDEN_ROLE`, 비멤버 접근 403 `NOT_MEMBER`
- **settings PATCH 화이트리스트 테스트**: 16개 키 각각 갱신 성공 / `briefing_timezone` 포함 시 400 `VALIDATION_ERROR`
- **transfer-answerer 테스트**: 교체 후 담당자가 정확히 1명이고 구담당자 role이 `asker`이며
  `projects.answerer_id`가 갱신됨. **단일 UPDATE로 구현했다면 이 테스트가 `23505`로 실패한다** — 순서대로 고칠 것
- **leave 테스트**: asker 탈퇴 성공 + 기존 데이터 보존 확인 / 담당자 탈퇴 시도 403 `FORBIDDEN_ROLE`
- 만료 토큰 401 `TOKEN_EXPIRED`
- `uv run pytest` 전체 통과 → 커밋 `feat(M1): auth, projects, members`
