"""既存の関数キャッシュをアプリの資源寿命へ結び付ける。"""

from functools import lru_cache, wraps
from hashlib import sha256

from rag_engine.resources.runtime import current_runtime


def application_cache(*, maxsize: int):
    """SDK 呼び出しは instance pool、互換直接呼び出しは従来の LRU を使う。"""

    def decorate(function):
        legacy = lru_cache(maxsize=maxsize)(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            runtime = current_runtime()
            if runtime is None or runtime.model_pool is None:
                return legacy(*args, **kwargs)
            signature = (args, tuple(sorted(kwargs.items())))
            key = function.__module__ + ":" + function.__name__ + ":" + sha256(repr(signature).encode()).hexdigest()
            holder = []

            def create():
                value = function(*args, **kwargs)
                holder.append(value)
                return value

            def release():
                value = holder.pop() if holder else None
                client = getattr(value, "client", value)
                close = getattr(client, "close", None)
                if callable(close):
                    close()
                else:
                    session = getattr(getattr(client, "base_client", None), "session", None)
                    if session is not None:
                        session.close()

            return runtime.model_pool.get(key, create, signature=signature, unloader=release)

        wrapped.cache_clear = legacy.cache_clear
        wrapped.cache_info = legacy.cache_info
        return wrapped

    return decorate
