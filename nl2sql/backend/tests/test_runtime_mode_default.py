"""NL2SQL_RUNTIME_MODE の既定（#897）。

既定は Oracle / Select AI を呼ぶ `oracle`。`deterministic` は CI / テスト用で、
`tests/conftest.py` が明示する。
"""

from pathlib import Path

from app.settings import Settings


def test_runtime_mode_default_is_oracle() -> None:
    assert Settings.model_fields["nl2sql_runtime_mode"].default == "oracle"


def test_env_example_uses_oracle_runtime() -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    lines = [line.strip() for line in example.read_text(encoding="utf-8").splitlines()]
    assert "NL2SQL_RUNTIME_MODE=oracle" in lines
