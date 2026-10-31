"""Select AI Credential が読む OCI config（user / tenancy / fingerprint / key_file）の補助。

OCI 認証の設定画面と platform の `pr_system_settings.oci` は削除した（#11）。Select AI の
Credential の作成だけが、サーバーに置いた OCI config から署名の材料を読むため、その読み取りを
ここに残す。Select AI の削除（#7）と一緒にこの module も削除する。
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

OCI_CONFIG_MAX_BYTES = 64 * 1024
OCI_CONFIG_KEYS = ("user", "fingerprint", "tenancy", "region", "key_file")


@dataclass(frozen=True)
class OciConfigProfile:
    """OCI config の profile から読んだ値。"""

    profile: str
    user: str = ""
    fingerprint: str = ""
    tenancy: str = ""
    region: str = ""
    key_file: str = ""


def oci_config_file(settings: Any) -> str:
    return str(getattr(settings, "oci_config_file", "") or "").strip() or "~/.oci/config"


def oci_profile(settings: Any) -> str:
    return str(getattr(settings, "oci_config_profile", "") or "").strip() or "DEFAULT"


def read_oci_config_text(config_file: str) -> str:
    """OCI config file を上限付きで読む。読めなければ HTTPException。"""
    path = Path(config_file).expanduser()
    try:
        if not path.is_file():
            raise HTTPException(status_code=404, detail="OCI config ファイルを読み取れません。")
        if path.stat().st_size > OCI_CONFIG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="OCI config ファイルが大きすぎます。")
        return path.read_text(encoding="utf-8")
    except HTTPException:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=404, detail="OCI config ファイルを読み取れません。"
        ) from exc


def parse_oci_config(content: str, profile: str) -> OciConfigProfile:
    """OCI config の profile の値を読む。形式・profile が不正なら HTTPException。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise HTTPException(status_code=400, detail="OCI config の形式が不正です。") from exc

    selected_profile = profile.strip() or "DEFAULT"
    if selected_profile.upper() == "DEFAULT":
        entries: Any = parser.defaults()
    elif parser.has_section(selected_profile):
        entries = parser[selected_profile]
    else:
        raise HTTPException(status_code=404, detail="OCI config の profile が見つかりません。")

    values = {key: str(entries.get(key, "")).strip() for key in OCI_CONFIG_KEYS}
    if not any(values.values()):
        raise HTTPException(status_code=422, detail="OCI config の項目を読み取れません。")
    return OciConfigProfile(profile=selected_profile, **values)


def resolve_oci_key_file(key_file: str, config_file: str | Path) -> Path:
    """OCI config の key_file を絶対 path へ解決する（相対なら config の隣）。"""
    path = Path(key_file).expanduser()
    if path.is_absolute():
        return path
    return Path(config_file).expanduser().parent / path


def pem_file_is_encrypted(path: Path) -> bool:
    """PEM の代表的な暗号化 marker を先頭だけ読んで検出する。"""
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = head.decode("utf-8", errors="ignore").upper()
    return "BEGIN ENCRYPTED PRIVATE KEY" in text or "PROC-TYPE: 4,ENCRYPTED" in text
