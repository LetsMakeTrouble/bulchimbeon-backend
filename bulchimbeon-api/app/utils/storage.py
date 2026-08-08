"""`STORAGE_DIR` 경로 결합 (`03 §5.2`).

⚠️ **`settings.storage_dir / version.storage_path` 를 직접 쓰지 않는다.**
`Path.__truediv__` 는 우변이 절대 경로면 **좌변을 조용히 버린다**:

    Path("/app/storage") / "/etc/passwd"  ==  PosixPath("/etc/passwd")

지금은 `document_service._store` 가 항상 상대 경로를 넣으므로 안전하지만,
M8(notion/github 동기화)이 다른 경로로 버전 행을 만들면 그 순간 STORAGE_DIR **밖**을
읽게 된다. 결합 지점을 한 곳으로 모으고 여기서 단언한다.
"""

from pathlib import Path

from app.config import settings


class StoragePathError(RuntimeError):
    """`STORAGE_DIR` 밖을 가리키는 `storage_path`. 데이터 손상이지 사용자 입력 오류가 아니다."""


def resolve_storage_path(storage_path: str) -> Path:
    """`document_versions.storage_path`(상대 경로)를 절대 경로로 만든다."""
    root = settings.storage_dir.resolve()
    resolved = (root / storage_path).resolve()

    if not resolved.is_relative_to(root):
        raise StoragePathError(
            f"storage_path 가 STORAGE_DIR 밖을 가리킨다: {storage_path!r} (root={root})"
        )
    return resolved
