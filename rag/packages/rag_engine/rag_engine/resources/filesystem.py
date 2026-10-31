"""指定された保存ルート内の成果物を読み書きする adapter。"""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class FileArtifactStore:
    """相対 key を保存先に解決する。ルート外参照を拒否する。"""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not key or Path(key).is_absolute() or not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("artifact key must be a relative path inside the root")
        return path

    def read(self, key: str) -> bytes:
        """成果物を読む。不在時は FileNotFoundError を返す。"""
        return self._path(key).read_bytes()

    def write(self, key: str, data: bytes) -> None:
        """一時ファイルと rename で既存成果物を原子的に置き換える。"""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
