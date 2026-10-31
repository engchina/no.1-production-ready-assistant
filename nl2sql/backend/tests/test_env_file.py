from __future__ import annotations

import threading
import time
from pathlib import Path

from app.env_file import locked_env_file, replace_env_file


def test_locked_env_file_serializes_concurrent_read_modify_replace(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BASE=1\n", encoding="utf-8")
    first_read = threading.Event()
    errors: list[BaseException] = []

    def append_assignment(key: str, value: str, *, pause_after_read: bool = False) -> None:
        try:
            with locked_env_file(env_file) as locked_path:
                lines = locked_path.read_text(encoding="utf-8").splitlines()
                if pause_after_read:
                    first_read.set()
                    time.sleep(0.05)
                next_lines = [*lines, f"{key}={value}"]
                replace_env_file(locked_path, "\n".join(next_lines) + "\n")
        except BaseException as exc:  # pragma: no cover - join 後に main thread で再送出する
            errors.append(exc)

    first = threading.Thread(
        target=append_assignment,
        args=("FIRST", "one"),
        kwargs={"pause_after_read": True},
    )
    second = threading.Thread(target=append_assignment, args=("SECOND", "two"))

    first.start()
    assert first_read.wait(timeout=2)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    if errors:
        raise AssertionError("env writer thread failed") from errors[0]

    content = env_file.read_text(encoding="utf-8")
    assert "BASE=1\n" in content
    assert "FIRST=one\n" in content
    assert "SECOND=two\n" in content
