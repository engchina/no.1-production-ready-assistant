"""OCI SDK 認証 config の非対話ロード補助。

RAG の `app.clients.oci_auth` と同じ import パス / 公開 API を NL2SQL 側でも維持する。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR = (  # nosec B105 - パスワードではなくエラーメッセージ定数
    "OCI API 秘密鍵 PEM が暗号化されています。"
    " pass_phrase を OCI config に設定するか、パスフレーズなしの秘密鍵 PEM を使用してください。"
)
PASSPHRASE_CONFIG_KEYS = frozenset({"pass_phrase", "passphrase", "key_password"})


class OciPrivateKeyPassPhraseRequiredError(RuntimeError):
    """OCI SDK が pass phrase を対話入力する前に止める。"""

    safe_for_user = True


def load_oci_config_without_prompt(
    oci_config_module: Any,
    config_file: str,
    profile: str,
    *,
    region: str | None = None,
) -> dict[str, Any]:
    """OCI config を読み、暗号化 PEM の対話プロンプトを事前に防ぐ。"""
    config_path = Path(config_file).expanduser()
    config = dict(oci_config_module.from_file(str(config_path), profile))
    if region:
        config["region"] = region
    assert_oci_private_key_can_load_without_prompt(config, config_path)
    return config


def assert_oci_private_key_can_load_without_prompt(
    config: Mapping[str, object],
    config_file: str | Path,
) -> None:
    """OCI SDK client 作成時に pass phrase prompt が出ない設定か確認する。"""
    key_file = str(config.get("key_file", "") or "").strip()
    if not key_file or _has_pass_phrase(config):
        return
    key_path = resolve_oci_key_file(key_file, config_file)
    if pem_file_is_encrypted(key_path):
        raise OciPrivateKeyPassPhraseRequiredError(OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR)


def resolve_oci_key_file(key_file: str, config_file: str | Path) -> Path:
    """OCI config の key_file を絶対 path へ解決する。"""
    path = Path(key_file).expanduser()
    if path.is_absolute():
        return path
    return Path(config_file).expanduser().parent / path


def pem_file_is_encrypted(path: Path) -> bool:
    """PEM の代表的な暗号化 marker を少量だけ読んで検出する。"""
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = head.decode("utf-8", errors="ignore").upper()
    return "BEGIN ENCRYPTED PRIVATE KEY" in text or "PROC-TYPE: 4,ENCRYPTED" in text


def _has_pass_phrase(config: Mapping[str, object]) -> bool:
    """OCI config に private key pass phrase が明示されているか判定する。"""
    return any(str(config.get(key, "") or "").strip() for key in PASSPHRASE_CONFIG_KEYS)


__all__ = [
    "OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR",
    "PASSPHRASE_CONFIG_KEYS",
    "OciPrivateKeyPassPhraseRequiredError",
    "assert_oci_private_key_can_load_without_prompt",
    "load_oci_config_without_prompt",
    "pem_file_is_encrypted",
    "resolve_oci_key_file",
]
