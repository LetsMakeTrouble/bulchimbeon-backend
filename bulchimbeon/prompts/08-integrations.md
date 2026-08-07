# M8 — Notion · GitHub 지식 원본 연동 (기능 1.5) — ⛔ 기본 제외

> **이 마일스톤은 컷라인이 아니라 "기본 제외"로 승격되었다** (결정 1.18 컷라인 1).
> 데모 스크립트 1~6단계 어디에도 등장하지 않으며, 1주 일정에서 가장 먼저 잘려야 하는 작업이다.
>
> **기본 산출물 (여유가 없을 때 = 정상 경로)**
> - `POST /projects/{id}/integrations` · `GET /projects/{id}/integrations` ·
>   `POST /integrations/{id}/sync` · `DELETE /integrations/{id}` 를 **스텁으로 등록**하고
>   **501 `NOT_IMPLEMENTED`**를 반환한다. 계약서 §5의 경로·요청 스키마는 그대로 두어
>   프론트가 "확장 포인트"로 화면을 잡을 수 있게 한다.
> - 발표에서는 "인터페이스는 열려 있고 구현은 다음 스프린트"로 말한다.
> - 소요 30분. 이것만으로 이 마일스톤은 **완료**로 간주한다.
>
> **아래 전체 구현은 M9(시드·배포·리허설)까지 전부 끝나고 시간이 남을 때만 착수한다.**
> M8에 손대느라 M9 리허설이 밀리면 데모 자체가 위험해진다.

## 작업 (여유가 있을 때만)

1. **모델·마이그레이션**: integrations — config의 토큰 필드는 Fernet(`INTEGRATION_ENCRYPTION_KEY`) 암호화 저장, 조회 응답에서는 마스킹(`ntn_****`)
2. **연동 API** (계약서 §5): 등록(provider별 config 스키마 검증), 목록, 삭제, `POST /integrations/{id}/sync`(202 + 백그라운드)
3. **GitHub 동기화** (`services/sync/github_sync.py`):
   - GitHub REST contents API(httpx) — `repo`/`branch`/`path_glob` 매칭 md 파일 수집, 공개 레포는 토큰 없이 동작
   - 파일 SHA 비교로 변경 감지 → 신규 파일=새 문서(source_type=github, source_ref=`owner/repo:path`), 변경=새 버전 → **기존 인제스트·활성화·재검토 연쇄 파이프라인 재사용** (룰 5 동일 적용 — 별도 경로 만들지 말 것)
4. **Notion 동기화** (`services/sync/notion_sync.py`): notion-client로 `page_ids` 블록 트리 → 마크다운 변환(헤딩·리스트·코드·테이블 기본만) → 동일 문서/버전 파이프라인. `last_edited_time` 비교로 변경 감지
5. 완료 시 `last_synced_at`/`last_sync_status` 갱신 + `sync.completed|failed` **알림**(`04 §4`) — **SSE는 `sync.completed`만**이며 payload는 `{integration_id, new_documents, new_versions}` (`05 §12.3`) + `sync.run` 이벤트
6. 실패 격리: 파일 1개 실패가 전체 동기화를 중단시키지 않도록 — 실패 목록을 status payload에

## 완료 기준

**스텁 경로 (기본)**
- 4개 엔드포인트가 계약서 §5의 경로·요청 스키마로 등록되어 Swagger에 보이고 501 `NOT_IMPLEMENTED`를 반환
- 커밋 `feat(M8): integration endpoints stub (501)`

**전체 구현 경로 (여유가 있을 때만)**
- GitHub: 목킹 테스트(파일 목록·SHA 변경 시나리오) + **실제 공개 레포 1회 e2e** (예: 본인 레포에 seed md 올려 테스트)
- Notion: notion-client 목킹으로 블록→마크다운 변환·버전 생성 테스트
- 동기화로 생긴 새 버전이 재검토 연쇄를 일으키는지 확인(M4 로직 재사용 검증)
- 토큰이 DB에 평문으로 저장되지 않음을 테스트로 확인
- `uv run pytest` 통과 → 커밋 `feat(M8): notion and github sync`
