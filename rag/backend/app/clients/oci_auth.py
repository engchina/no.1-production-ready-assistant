"""OCI SDK 認証 config の非対話ロード補助(re-export shim)。

正本は共有 package `rag_parser_core.oci_auth`。backend は本モジュール経由で従来の
import パス(`app.clients.oci_auth`)を維持する。parser マイクロサービスは共有 core を
直接 import する。
"""

from rag_parser_core.oci_auth import (
    OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR,
    PASSPHRASE_CONFIG_KEYS,
    OciPrivateKeyPassPhraseRequiredError,
    assert_oci_private_key_can_load_without_prompt,
    load_oci_config_without_prompt,
    pem_file_is_encrypted,
    resolve_oci_key_file,
)

__all__ = [
    "OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR",
    "PASSPHRASE_CONFIG_KEYS",
    "OciPrivateKeyPassPhraseRequiredError",
    "assert_oci_private_key_can_load_without_prompt",
    "load_oci_config_without_prompt",
    "pem_file_is_encrypted",
    "resolve_oci_key_file",
]
