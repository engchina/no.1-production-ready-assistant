"""解析モデルをプロセス内に常駐させる簡易プール（エンジンごとのモデル常駐管理）。

一度ロードしたモデルは、UI から解放するまでプロセス内（GPU 利用時は VRAM 上）に置いたままにする。
同じエンジンを繰り返し試すときに毎回ロードし直すのを避けるためのもの。
"""

from __future__ import annotations

import gc
import importlib.util
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from rag_engine.resources.runtime import current_runtime

_lock = threading.Lock()
_entries: dict[str, tuple[Any, Any, Callable[[], None] | None]] = {}  # key -> (signature, obj, unloader)
# ponytail: プロセス全体で 1 個のロックを取る。ロード中は他エンジンのロードも待つが、比較ラボの規模なら十分。


def get(key: str, loader: Callable[[], Any], *, signature: Any = None, unloader: Callable[[], None] | None = None) -> Any:
    """常駐済みならそれを返し、無ければ loader でロードして常駐させる。signature が変わっていたら作り直す。"""
    runtime = current_runtime()
    pool = runtime.model_pool if runtime is not None else None
    if pool is not None:
        return pool.get(key, loader, signature=signature, unloader=unloader)
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry[0] == signature:
            return entry[1]
        if entry is not None:
            _drop(key)
        obj = loader()
        _entries[key] = (signature, obj, unloader)
        return obj


def unload(key: str) -> bool:
    """指定 key の常駐モデルを unloader 実行後にプールから外します。"""
    runtime = current_runtime()
    pool = runtime.model_pool if runtime is not None else None
    if pool is not None:
        return pool.unload(key)
    with _lock:
        existed = _drop(key)
    trim_cuda_cache()
    return existed


def unload_all() -> list[str]:
    """全ての常駐モデルを解放し、プールを空にします。"""
    runtime = current_runtime()
    pool = runtime.model_pool if runtime is not None else None
    if pool is not None:
        return pool.unload_all()
    with _lock:
        keys = list(_entries)
        for key in keys:
            _drop(key)
    trim_cuda_cache()
    return keys


def loaded() -> list[str]:
    """現在プールに常駐している key の一覧を返します。"""
    runtime = current_runtime()
    pool = runtime.model_pool if runtime is not None else None
    if pool is not None:
        return pool.loaded()
    with _lock:
        return sorted(_entries)


def trim_cuda_cache() -> None:
    """解放済みだが PyTorch が確保したままの VRAM を返す（他プロセスから見える空き容量を戻す）。"""
    gc.collect()
    if importlib.util.find_spec("torch") is None:
        return
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def gpu_summary() -> str:
    """このプロセスと GPU 全体の VRAM 使用量を 1 行で返す。"""
    parts: list[str] = []
    if importlib.util.find_spec("torch") is not None:
        import torch

        if torch.cuda.is_available():
            allocated = torch.cuda.memory_allocated() / 2**30
            reserved = torch.cuda.memory_reserved() / 2**30
            parts.append(f"このプロセス: 使用 {allocated:.2f} GiB / 確保 {reserved:.2f} GiB")
        else:
            parts.append("CUDA は利用できません（CPU 実行）")
    try:
        out = (
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            .stdout.strip()
            .splitlines()[0]
        )
        name, used, total = [part.strip() for part in out.split(",")]
        parts.append(f"GPU 全体 ({name}): {int(used) / 1024:.2f} / {int(total) / 1024:.2f} GiB 使用中")
    except Exception:
        pass
    return " ｜ ".join(parts) if parts else "GPU 情報を取得できません。"


def _drop(key: str) -> bool:
    entry = _entries.pop(key, None)
    if entry is None:
        return False
    _, obj, unloader = entry
    if unloader is not None:
        with suppress(Exception):
            unloader()
    del obj
    return True
