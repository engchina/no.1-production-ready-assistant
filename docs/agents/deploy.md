# 配備

> root の [AGENTS.md](../../AGENTS.md) から移した詳細（#2276）。root の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、root には規則の要約と案内だけを書く。
> 正本: `terraform/stack/`・`platform/deploy/`・[terraform/README.md](../../terraform/README.md)。

- 自前のコードは Docker を使わずネイティブで動かす（開発は `uv run` / `npm run dev`、本番は systemd。Dockerfile・compose は持たない。Agent も組み込み Runtime で、Docker の Runtime は持たない）。
- **再配備は、配備済み Compute の repo の直下で `sudo -n ./platform/deploy/suite-update.sh` を実行する。** `origin/main` を取得し、保存済みの設定・Wallet を保って build・サービス再起動・health 確認を行う。`-n` はパスワード入力を待たずに実行する指定。
- Terraform stack は `terraform/stack/` に 1 つだけ。ADB を 1 つ作り（新規 / 既存）、選んだ製品（`deploy_rag` / `deploy_nl2sql` / `deploy_agent`、最低 1 つ）を 1 台の Compute に入れる。製品固有の入力は `rag_` / `nl2sql_` / `agent_` を付ける。
- Compute の上では `platform/deploy/suite-init.sh` が各製品の `init_script.sh`（製品は単独では配備しない）を順に呼ぶ。**reverse proxy（Nginx など）・TLS の証明書・公開の port は配備しない**（#19。前段に置くかは利用者の選択。各製品は backend を `127.0.0.1` の製品の port で、frontend は `dist/` を build するだけ。手順は [README](../../README.md)「各製品の公開」）。frontend の base は build の `FRONTEND_BASE_PATH`（未指定は `/`。配備は `/<製品>/`）。
- **3 製品は既定で ADB のアプリ用の DB ユーザー（ADMIN ではない。既定の名前 `ADB_APP_USER`）で接続する**（#1668・#1675）。ユーザーの作成と grant は Compute の `suite-init.sh` が製品の配備の前に ADMIN で行い（`platform/deploy/suite-db-user.py`）、Terraform からは DB に接続しない（ADB は Compute からしか届かない）。DB の新しい機能を使うときは、付ける権限（`PDB_DBA` と Deep Data Security の 3 つ）で足りるかを確かめ、[terraform/README.md](../../terraform/README.md)「アプリ用の DB ユーザー」の表を更新する。
- **Compute の OS は Oracle Linux 9（image の OCID は `terraform/stack` で固定）、ログインと 3 製品のプロセスのユーザーは `opc`**（#1611）。配備のスクリプトは `dnf`・SELinux（enforcing のまま。context と boolean は `suite-init.sh` の `configure_selinux`）を前提にし、Ubuntu の分岐を持たない。
- **製品の systemd の unit・サービスの名前を改名・削除する PR では、同じ PR で次をそろえる**（#2493。#2457 の改名で既存の Compute に新しい unit が入らなかった: #2484・#2486）。
  - 再配備で旧い unit を止めて消し、sudoers の許可も外す。
  - 改名では、新しい unit を初めて作るときに旧い unit の起動 / 停止（enable / disable）を引き継ぐ。利用者が停止したサービスを起動しない。
  - 既存の環境に残る配備の一覧・設定（`rag_services.txt` など）を新しい名前へ移す。stack が常に配備するサービスは再配備の補完（`REQUIRED_RAG_SERVICES` など）にも入れ、一覧から消えた名前が戻らない経路を残さない。
  - 回帰テストを足す: 旧い一覧・unit からの更新、同じ配備の繰り返し、最新の stack の新規配備。`terraform/scripts/verify_stack_contract.py` の契約も合わせる。
  - 配備しなくなった unit の削除と状態の引継ぎは今は `rag/init_script.sh` だけが持つ。NL2SQL / Agent の unit を初めて改名・削除するときは、`platform/deploy/` の共通の関数にする（#2494）。
- CI は `Suite / Deploy`（`suite-deploy.yml`。release も再利用する）が各製品の `init_script.sh` と配備のスクリプト・`terraform/scripts/package_stack.py`・`verify_stack_contract.py`・`platform/deploy/tests/` を 1 つの job で実行し、`Suite / Oracle Linux 9` が `oraclelinux:9` の container で package の名前を確かめる（`Suite / Deploy` は製品の frontend・共有 UI・Markdown だけの変更では動かない。#1592）。実際の配備の通しは `deploy-nightly.yml` が毎晩、`oraclelinux:9` の systemd の container で `suite-init.sh` を実行して 3 製品の backend の health と frontend の build を確かめる（Oracle・SELinux・OCI の network は対象外。理由は `.github/scripts/deploy-run-through.sh` の冒頭。#1595）。
- release tag は `suite-v*`（`terraform-release.yml` が zip と sha256 と provenance を公開する）。release する commit は main にあり、その commit で main の CI が成功している必要がある（手動の実行は main から。#1594）。製品ごとの release（`<製品>-v*`）は作らない（既存の `nl2sql-v*` は残す）。README の配備ボタンは `releases/latest/download/<asset>` を指す（asset 名は変えない。新しい release を GitHub の Latest にする）。
