#!/usr/bin/env bash
# scripts/third-party-notices.py のテスト（#3392）。一時的な git リポジトリに合成の lockfile と通知の表を置いて確かめる。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT_DIR}/scripts/third-party-notices.py"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

repo="${WORK}/repo"
mkdir -p "${repo}/app/backend" "${repo}/app/frontend"
git -C "${repo}" init -q

cat >"${repo}/app/backend/uv.lock" <<'EOF'
version = 1
requires-python = "==3.12.*"

[[package]]
name = "app-backend"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "lib-a" },
]

[package.optional-dependencies]
extra-x = [
    { name = "lib-c" },
]

[package.dev-dependencies]
dev = [
    { name = "lib-dev" },
]

[[package]]
name = "lib-a"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "lib-b", extra = ["fast"] },
]

[[package]]
name = "lib-b"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

[package.optional-dependencies]
fast = [
    { name = "lib-d" },
]

[[package]]
name = "lib-c"
version = "3.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "lib-d"
version = "4.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "lib-dev"
version = "5.0.0"
source = { registry = "https://pypi.org/simple" }
EOF

cat >"${repo}/app/frontend/package-lock.json" <<'EOF'
{
  "name": "app-frontend",
  "lockfileVersion": 3,
  "packages": {
    "": { "name": "app-frontend" },
    "node_modules/js-mit": { "version": "1.0.0", "resolved": "https://registry.npmjs.org/js-mit/-/js-mit-1.0.0.tgz", "license": "MIT" },
    "node_modules/js-or": { "version": "1.0.0", "resolved": "https://registry.npmjs.org/js-or/-/js-or-1.0.0.tgz", "license": "(MIT OR GPL-3.0)" },
    "node_modules/font-a": { "version": "1.0.0", "resolved": "https://registry.npmjs.org/font-a/-/font-a-1.0.0.tgz", "license": "OFL-1.1" },
    "node_modules/tool-x-linux": { "version": "1.0.0", "resolved": "https://registry.npmjs.org/tool-x-linux/-/x.tgz", "license": "MPL-2.0", "dev": true },
    "node_modules/@shared/ui": { "resolved": "../../shared/ui", "link": true }
  }
}
EOF
git -C "${repo}" add -A

# inventory: runtime と extra を辿り、dev は --all のときだけ。自分の project・リンクは出さない。
inventory="$(python3 "${SCRIPT}" --repo "${repo}" inventory)"
for want in "python,app/backend/uv.lock,lib-a,1.0.0,runtime," "python,app/backend/uv.lock,lib-d,4.0.0,runtime," \
  "python,app/backend/uv.lock,lib-c,3.0.0,extra:extra-x," "npm,app/frontend/package-lock.json,tool-x-linux,1.0.0,dev,MPL-2.0"; do
  grep -qxF "${want}" <<<"${inventory}" || fail "inventory に ${want} がありません"
done
grep -q "lib-dev\|app-backend\|@shared/ui" <<<"${inventory}" && fail "inventory に dev・自分の project・リンクが出ています"
python3 "${SCRIPT}" --repo "${repo}" inventory --all | grep -q "lib-dev,5.0.0,dev:dev" || fail "--all で dev が出ません"

# check: 表の部品が依存にあり、npm の permissive でないものが表にあれば成功（glob も使える）。
cat >"${repo}/THIRD-PARTY-NOTICES.md" <<'EOF'
| 部品 | 取得元 | ライセンス |
|---|---|---|
| `lib_d` | PyPI | LGPL-2.1 |
| `lib-dev` | PyPI | MPL-2.0 |
| `font-a` | npm | OFL-1.1 |
| `tool-x-*` | npm | MPL-2.0 |
| `libreoffice` | OS（dnf） | MPL-2.0 |
EOF
python3 "${SCRIPT}" --repo "${repo}" check >/dev/null || fail "合っている表で check が失敗しました"

# 依存から外れた部品が表に残っていれば失敗する。
cat >>"${repo}/THIRD-PARTY-NOTICES.md" <<'EOF'
| `lib-removed` | PyPI | GPL-2.0 |
EOF
if out="$(python3 "${SCRIPT}" --repo "${repo}" check 2>&1)"; then fail "外れた部品で check が成功しました"; fi
grep -q "lib-removed" <<<"${out}" || fail "外れた部品の名前がエラーにありません: ${out}"

# npm の permissive でない package が表に無ければ失敗する。
sed -i '/lib-removed/d; /font-a/d' "${repo}/THIRD-PARTY-NOTICES.md"
if out="$(python3 "${SCRIPT}" --repo "${repo}" check 2>&1)"; then fail "表に無い OFL の package で check が成功しました"; fi
grep -q "font-a（OFL-1.1）" <<<"${out}" || fail "表に無い package の名前がエラーにありません: ${out}"

# repo の実際の表と lockfile も合っている。
python3 "${SCRIPT}" check >/dev/null || fail "repo の THIRD-PARTY-NOTICES.md と lockfile が合っていません"

echo "ok: third-party-notices"
