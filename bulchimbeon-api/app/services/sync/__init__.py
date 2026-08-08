"""외부 지식 원본 동기화 (M8, `08`).

- `base.py` — 두 provider 가 공유하는 계약(`RemoteRef` · `StoredVersion` · `SyncProvider`)
- `github_sync.py` / `notion_sync.py` — 원격에서 열거·변경판정·본문 수집만 한다
- `runner.py` — 오케스트레이션. **문서/버전 생성부터는 M2 인제스트와 M4 재검토 연쇄를
  그대로 태운다** (`08 §3` 3번, 룰 5). 여기에 별도 인제스트 경로를 만들지 않는다.
"""
