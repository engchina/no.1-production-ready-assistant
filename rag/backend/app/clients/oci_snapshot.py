"""OCI SDK のモデルを製品の制限付き snapshot 用の値へ変換する。"""

import importlib


def oci_call_snapshot(value: object) -> object:
    if hasattr(value, "swagger_types"):
        return importlib.import_module("oci.util").to_dict(value)
    return value
