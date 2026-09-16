"""Owner-only, atomic storage for session state and application drafts."""
import json
import os
from pathlib import Path
import tempfile


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private_json(path: Path, value: dict) -> None:
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".hh-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
