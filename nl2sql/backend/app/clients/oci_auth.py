"""OCI SDK 認証 config の非対話ロード補助。実装は platform の pr_system_settings（#100）。"""

from pr_system_settings.oci_auth import *  # noqa: F403
from pr_system_settings.oci_auth import (  # noqa: F401
    OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR,
    PASSPHRASE_CONFIG_KEYS,
    assert_oci_private_key_can_load_without_prompt,
    load_oci_config_without_prompt,
    pem_file_is_encrypted,
    resolve_oci_key_file,
)
