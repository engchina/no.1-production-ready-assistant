# 既存の OCI Compute を monorepo 構成へ移行する手順書

対象: `engchina/no.1-production-ready-suite#71`（4 repo を monorepo に統合）の切替後に、
**既に稼働している** NL2SQL の OCI Compute を新しい配置へ1回だけ移す手順。
新規に作る Compute は Terraform stack（`nl2sql-v0.1.32` 以降）が最初から新しい配置で作るため、この手順は不要。

## 1. 何が変わるか

| | 旧構成（〜v0.1.31） | 新構成（nl2sql-v0.1.32〜） |
|---|---|---|
| NL2SQL | `/u01/aipoc/no.1-production-ready-nl2sql`（単独 repository） | `/u01/aipoc/no.1-production-ready-suite/nl2sql` |
| 共有 platform | `/u01/aipoc/no.1-production-ready-platform`（単独 repository） | `/u01/aipoc/no.1-production-ready-suite/platform` |
| Git 操作 | 2つの repository をそれぞれ pull | suite を1回 pull |
| 更新コマンド | `cd /u01/aipoc/no.1-production-ready-nl2sql && ./scripts/git-pull.sh && sudo ./scripts/update-after-pull.sh` | `cd /u01/aipoc/no.1-production-ready-suite/nl2sql && ./scripts/git-pull.sh && sudo ./scripts/update-after-pull.sh` |

変わらないもの: `/u01/aipoc/wallet`、`/u01/aipoc/props`、`/u01/aipoc/recovery`、`/u01/data/production-ready-nl2sql`、
systemd unit 名、Nginx の site 名、DB スキーマ、`backend/.env` の中身。

> #211 以降の版では、3製品共通の設定（`PLATFORM_*`）を `platform/.env` に置き、旧名は読まない。
> この手順で `backend/.env` を持ち越したあと、[configuration.md](./configuration.md) の
> 「既存環境の更新手順（#211）」で `platform/.env` へ移してから `update-after-pull.sh` を実行する。

**切替後は旧構成のまま `git-pull.sh` / `update-after-pull.sh` を使わないこと。** 旧 NL2SQL repository は archive 済みで更新されない。
さらに旧 platform の URL は suite へ redirect され、suite は platform の履歴を引き継いでいるため、旧 `git-pull.sh` は
旧 platform の checkout に suite 全体（`platform/` `rag/` `nl2sql/` `agent/`）を fast-forward で取り込んでしまう。
その結果、旧 `update-after-pull.sh` は共有 UI（`packages/ui`）を見つけられずに失敗する。

`update-after-pull.sh` は systemd unit と Nginx 設定を書き換えないため、この手順で**パスを手動で差し替える**。

## 2. 前提

- 切替が完了している（suite の `main` に #71 が merge 済み、`nl2sql-v0.1.32` release が公開済み）。
- `ubuntu` ユーザーで SSH でき、`sudo` を使える。
- Autonomous Database が `Available`（`update-after-pull.sh` が migration を実行するため）。
- 作業中は短時間の停止が発生する（`update-after-pull.sh` の maintenance window と同じ程度）。

以下のコマンドは `ubuntu` で実行する。`sudo` が付いたものだけ root 権限を使う。

```bash
OLD_APP=/u01/aipoc/no.1-production-ready-nl2sql
OLD_PLATFORM=/u01/aipoc/no.1-production-ready-platform
SUITE=/u01/aipoc/no.1-production-ready-suite
NEW_APP="${SUITE}/nl2sql"
BACKUP_DIR="/u01/aipoc/recovery/monorepo-migration-$(date +%Y%m%d%H%M%S)"
```

## 3. 事前確認（何も変更しない）

```bash
# 3-1. 旧構成の状態を記録する
git -C "${OLD_APP}" status --short --branch
git -C "${OLD_APP}" log -1 --format='%h %cs %s'
git -C "${OLD_PLATFORM}" log -1 --format='%h %cs %s'

# 3-2. 旧構成が正常であることを確認する
cd "${OLD_APP}" && ./scripts/update-after-pull.sh --check

# 3-3. Git 管理外で新構成へ持ち越す必要があるファイルを洗い出す
#      通常は backend/.env だけ。frontend/dist と .venv は持ち越さず作り直す（dist は下の 5-3 で退避用にコピーする）。
git -C "${OLD_APP}" status --ignored --porcelain | grep -vE '(node_modules|\.venv|__pycache__|\.mypy_cache|\.ruff_cache|\.pytest_cache)/'

# 3-4. systemd unit と Nginx 設定に旧パスが書かれている箇所を確認する
sudo grep -n "${OLD_APP}" /etc/systemd/system/production-ready-nl2sql-*.service /etc/nginx/sites-available/production-ready-nl2sql
```

3-3 で `backend/.env` 以外のファイル（例: 手動で置いた証明書や設定）が見つかった場合は、5-2 で同じ相対パスへコピーする。
3-4 の出力に `${OLD_APP}` 以外の旧パス（例: `${OLD_PLATFORM}`）が出た場合も、6-2 の置換対象に加える。

## 4. バックアップ

```bash
sudo install -d -m 0700 "${BACKUP_DIR}"
sudo cp -a /etc/systemd/system/production-ready-nl2sql-*.service "${BACKUP_DIR}/"
sudo cp -a /etc/nginx/sites-available/production-ready-nl2sql "${BACKUP_DIR}/"
sudo cp -a "${OLD_APP}/backend/.env" "${BACKUP_DIR}/backend.env"
```

## 5. 新構成を用意する（稼働中のサービスには影響しない）

```bash
# 5-1. suite を clone する（/u01/aipoc は root:ubuntu 0775 なので ubuntu で作成できる）
git clone --branch main https://github.com/engchina/no.1-production-ready-suite.git "${SUITE}"
git -C "${SUITE}" log -1 --format='%h %cs %s'

# 5-2. backend/.env を持ち越す（権限は 0600、所有者は ubuntu）
install -m 0600 -o ubuntu -g ubuntu "${OLD_APP}/backend/.env" "${NEW_APP}/backend/.env"

# 5-3. 現在の frontend/dist をコピーする
#      update-after-pull.sh は新しい dist の公開に失敗したとき直前の dist へ戻すため、戻し先を用意しておく。
cp -a "${OLD_APP}/frontend/dist" "${NEW_APP}/frontend/dist"

# 5-4. backend の依存を先に同期する（update-after-pull.sh と同じコマンド）
#      6 で unit のパスを差し替えた後にサービスが再起動しても、新しい .venv で起動できるようにする。
cd "${NEW_APP}/backend" && /usr/local/bin/uv sync --locked --no-dev --python 3.12

# 5-5. 新構成の固定パス・権限・unit を読み取り専用で確認する
cd "${NEW_APP}" && ./scripts/update-after-pull.sh --check
```

## 6. 切り替える

```bash
# 6-1. 旧パスを新パスへ置き換える（unit と Nginx 設定）
sudo sed -i "s#${OLD_APP}/#${NEW_APP}/#g" \
  /etc/systemd/system/production-ready-nl2sql-*.service \
  /etc/nginx/sites-available/production-ready-nl2sql
sudo grep -n "${OLD_APP}" /etc/systemd/system/production-ready-nl2sql-*.service /etc/nginx/sites-available/production-ready-nl2sql \
  && echo "旧パスが残っています。6-1 を見直してください。" || echo "旧パスは残っていません。"

# 6-2. 反映する
sudo systemctl daemon-reload
sudo nginx -t && sudo systemctl reload nginx

# 6-3. 新構成で再デプロイする（backend/worker を新しい WorkingDirectory で再起動し、frontend を build して公開する）
cd "${NEW_APP}" && sudo ./scripts/update-after-pull.sh
```

6-3 が失敗した場合はエラー出力と `/var/log/nl2sql-update.log` を確認する。
DB に接続できないなど原因が明確で解消できる場合は、解消後に 6-3 だけを再実行してよい。
解消できない場合は「8. ロールバック」を行う。

## 7. 確認

```bash
cd "${NEW_APP}" && ./scripts/tail-logs.sh --status
systemctl show -p WorkingDirectory production-ready-nl2sql-backend   # ${NEW_APP}/backend であること
curl -fsS http://127.0.0.1/api/health
```

- ブラウザで画面を開き、ログインと主要画面（SQL 生成、実行履歴、システム設定）が表示されることを確認する。
- 問題がなければ、旧 repository を**削除せず改名**しておく（誤って旧パスを参照したときに気づけるようにするため）。

```bash
sudo mv "${OLD_APP}" "${OLD_APP}.pre-monorepo"
sudo mv "${OLD_PLATFORM}" "${OLD_PLATFORM}.pre-monorepo"
cd "${NEW_APP}" && ./scripts/update-after-pull.sh --check
```

- 1〜2週間問題がなければ `*.pre-monorepo` と `${BACKUP_DIR}` を削除する。

以後の更新は次のコマンドで行う。

```bash
cd /u01/aipoc/no.1-production-ready-suite/nl2sql &&
./scripts/git-pull.sh && sudo ./scripts/update-after-pull.sh
```

## 8. ロールバック

6 以降で問題が起きた場合に、旧構成へ戻す。

```bash
# 旧 repository を改名済みの場合は元に戻す
[ -d "${OLD_APP}.pre-monorepo" ] && sudo mv "${OLD_APP}.pre-monorepo" "${OLD_APP}"
[ -d "${OLD_PLATFORM}.pre-monorepo" ] && sudo mv "${OLD_PLATFORM}.pre-monorepo" "${OLD_PLATFORM}"

# unit と Nginx 設定をバックアップから戻す
sudo cp -a "${BACKUP_DIR}"/production-ready-nl2sql-*.service /etc/systemd/system/
sudo cp -a "${BACKUP_DIR}/production-ready-nl2sql" /etc/nginx/sites-available/production-ready-nl2sql
sudo systemctl daemon-reload
sudo nginx -t && sudo systemctl reload nginx

# 旧構成のコードのまま、サービスを再起動する（ソースは取得しない）
cd "${OLD_APP}" && sudo ./scripts/update-after-pull.sh --repair-only
```

- DB migration は冪等で、旧バージョンのコードでも動作する範囲に限られる（`update-after-pull.sh` はスキーマを作り直さない）。
- ロールバック後も `${SUITE}` は残してよい。原因を解消してから 5-5 以降をやり直す。

## 9. OCI Resource Manager の stack を更新する場合

既存の Compute は cloud-init を作成時にしか実行しないため、**稼働中の Compute に対してこの節の作業は不要**。
stack を新しい zip（`nl2sql-v0.1.32` 以降）へ更新して Compute を作り直す場合だけ、次に注意する。
（2026-09 以降は3製品共通の統合 stack（`suite-v*`、[terraform/README.md](../../terraform/README.md)）に移行した。新しく作る場合は統合 stack を使い、製品ごとの stack からの移行は同 README の「製品ごとの stack からの移行」を参照する。）

- 新しい stack には `platform_git_url` / `platform_git_ref` 変数がない（suite を1回 clone するため）。
- stack に保存済みの `application_git_url` が旧 URL（`https://github.com/engchina/no.1-production-ready-nl2sql.git`）のままだと、
  archive 済みの NL2SQL 単独 repository を suite の位置に clone してしまい、起動に失敗する。
  `application_git_url` を `https://github.com/engchina/no.1-production-ready-suite.git` に変更してから apply する。
  この変数は Resource Manager の画面では非表示なので、OCI CLI で更新する。

```bash
oci resource-manager stack get --stack-id <stack OCID> --query 'data.variables' > variables.json
# variables.json の application_git_url を suite の URL に変更し、platform_git_url / platform_git_ref を削除する
oci resource-manager stack update --stack-id <stack OCID> --variables file://variables.json
```
