# デプロイメント

## ローカル開発

設定は2つの `.env` に分かれる（#211）。

| ファイル | 内容 | 変数名 |
|---|---|---|
| `platform/.env`（雛形 `platform/.env.example`） | 3製品共通の設定（OCI 認証・アップロード保存先・モデル・データベース）。システム設定画面の保存先。`model-settings.json` も同じディレクトリに置き、3製品で共有する | `PLATFORM_*` |
| `rag/backend/.env`（雛形 `rag/backend/.env.example`） | RAG 固有の設定（認証・文書解析・検索・回答・HuggingFace など）。「文書解析」画面で保存した解析エンジンと接続は同じディレクトリの `parser-settings.json`（`RAG_PARSER_SETTINGS_FILE`。#1701）に置き、共有の `model-settings.json` には書かない | `RAG_*` |

backend は 環境変数 → `platform/.env` → `backend/.env` の順に読む（環境変数が最優先）。共通 `.env` の場所は
`PLATFORM_ENV_FILE` で上書きできる（既定はリポジトリの `platform/.env`）。旧名（接頭辞のない名前や `HF_TOKEN` /
`HF_ENDPOINT`）は読まない。既存環境は「[既存環境の更新手順（#211）](#既存環境の更新手順211-共通-env-への移行)」で移行する。

```bash
cp ../platform/.env.example ../platform/.env   # リポジトリ root から見て platform/.env（初回だけ）
cd backend
cp .env.example .env
uv sync   # 外部 parser は services/parsers/<name> の独立サービスで動く(backend には載せない)。
          # 単一 adapter をローカルで試す場合のみ per-adapter extra(例: `uv sync --extra docling`)。
uv run uvicorn app.main:app --reload

cd ../frontend
npm ci
BACKEND_URL=http://localhost:8000 npm run dev   # BACKEND_URL 未指定なら /api は proxy せず 404（hermetic）
```

一括起動はリポジトリ root で `./rag/scripts/start-all.sh` を実行する。最初に backend の
`uv sync`（初回は Sudachi 辞書の構築を含む）が完了するまで待ち、その後に API を起動する。
`BACKEND_READY_TIMEOUT_SECONDS`（既定 300 秒）は API の `/api/health` の待機だけに適用され、
依存準備の時間は含まない。待つ間は `BACKEND_READY_PROGRESS_SECONDS`（既定 15 秒）ごとに経過を出し、
backend のプロセスが終了するか上限を過ぎたときだけ失敗にする（負荷の高い端末でも起動を待ち切る。#1935）。準備中も Ctrl+C で子プロセスをまとめて停止できる。

NL2SQL と同じく、起動の判定には DB の readiness ではなくアプリの `/api/health` を使う。
ADB が停止中でも API と画面は起動できる。DB を使う一覧・検索・取込は利用できず、
取込 worker の Oracle 接続エラーは DB 復旧まで出ることがある。起動スクリプトは ADB を起動しない。

企業プロキシを利用する場合は、起動するターミナルで HTTP / HTTPS のプロキシも有効にする
（SSH だけの設定は npm に適用されない）。frontend の起動スクリプトは OS の CA bundle（Ubuntu の
`/etc/ssl/certs/ca-certificates.crt`、無ければ Oracle Linux / RHEL の `/etc/pki/tls/certs/ca-bundle.crt`）を
`NODE_EXTRA_CA_CERTS` に設定し、OS に登録済みの企業 CA を npm の証明書検証にも使う。別の CA ファイルが必要なら `NODE_EXTRA_CA_CERTS` を明示する。
指定済みの値は上書きせず、`strict-ssl=false` や `NODE_TLS_REJECT_UNAUTHORIZED=0` は使わない。
ネットワークの規則で公開 npm registry が禁止されている場合は、組織の推奨 registry を
`npm_config_registry` で指定する。内部 registry を VPN で直接利用する構成では、そのホストを
`no_proxy` / `NO_PROXY` に加える。証明書の追加だけではアクセス制限は解消しない。

Python の依存準備にも同じ HTTP(S) proxy が必要。ローカルの `rag-parser-core` などの隔離 build は
`build-system.requires` の `hatchling` を別に解決するため、既存の venv があっても PyPI への接続が
必要になることがある。`Failed to resolve requirements from build-system.requires` と接続 timeout が
出た場合は、起動するのと同じ terminal で組織の proxy 設定を有効にする。利用者が
`~/proxy-switch.sh` を使う環境なら、次のように実行する（helper の無い環境では組織の設定を使う）。

```bash
source ~/proxy-switch.sh
proxy on
./rag/scripts/start-all.sh
```

依存準備だけを確認する場合は `./rag/scripts/start-backend.sh --prepare-only`。
接続の timeout は parser のコードのエラーとは区別し、build isolation や TLS 検証を無効にして
回避しない。起動スクリプトは指定済みの proxy を継承し、社内 proxy の宛先を固定しない。
ポートの掃除は TCP listener だけを停止し、health の問い合わせや接続側の client は停止しない。

backend から同じマシン・private network のサービス（前処理・parser・pipeline の stage・サービス管理の
health の確認）への HTTP は、`HTTP_PROXY` を設定していてもプロキシを通さない（#852。判定は
`pr_backend_core.internal_http`）。`scripts/start-backend.sh` も、既存の値を残したまま `NO_PROXY` /
`no_proxy` に `localhost,127.0.0.1,::1` を足して backend を起動する。以前はプロキシが 127.0.0.1 へ届けられず
`502 cannotconnect` を返し、未起動の pipeline の stage を「サービスの応答した失敗」と見て検索が止まっていた。
内部の宛先を名前（`127.0.0.1` 以外のホスト名）で設定する場合は、そのホストを `NO_PROXY` に加える。

### 前処理 / parser（uv の venv + systemd。#286）

前処理と parser はサービスごとの uv の venv（`uv sync --locked --no-dev --python 3.12`）で動くネイティブのプロセスで、
`127.0.0.1:<port>` だけで listen する。本番と同じく systemd の unit（`production-ready-rag-<service>.service`）で動かし、
「運用設定 › サービス管理」画面から起動 / 停止する。Docker は使わない。unit の定義は本番の `init_script.sh` と共通
（[`scripts/rag-systemd.sh`](../scripts/rag-systemd.sh)）。

| サービス | ディレクトリ | port |
|---|---|---|
| preprocess-office-to-pdf / csv-to-jsonl / excel-to-jsonl | `services/preprocess/<name>` | 18010・18012・18013 |
| preprocess-word-to-jsonl（Word→JSONL。既定で配備・起動。#2457） | `services/preprocess/word_to_jsonl` | 18017 |
| parser-docling（既定の解析エンジン） | `services/parsers/docling` | 18020 |
| parser-unstructured（既定では配備しない。Docling が扱えない形式を取り込む場合） | `services/parsers/unstructured` | 18022 |

音声・動画は取り込まない（アップロードで止める。#3369）。GPU の ASR の parser と、OCI Generative AI（Vision）の parser は
#2042 で削除した。ファイル準備の「PDF を画像として読み直す」（`preprocess-pdf-to-page-images`、18011）は #3347 で、
取込時の「PIIマスク」（`preprocess-pii-redact`、18016）は #3358 で削除した。構造化レコード JSONL の解析エンジン
（`parser-structured-records-jsonl`、18023）と XML ツリー JSON の解析エンジン（`parser-xml-tree-json`、18021）は #21 で削除した。

backend の URL 設定（`RAG_PARSER_*_SERVICE_URL` / `RAG_PREPROCESS_*_SERVICE_URL`）の既定はこの `127.0.0.1:<port>`。
`backend/.env` に以前の Docker Compose の service 名（`http://parser-docling:8000` など）が残っている場合は `127.0.0.1:<port>` に直す（#356 から読み替えない）。

1. OS のコマンドを入れる（開発の Ubuntu / WSL。使うサービスの分だけでよい。本番の Compute（Oracle Linux 9）では `init_script.sh` が dnf で
   `libreoffice-core` / `-writer` / `-impress` / `-calc`・`google-noto-sans-cjk-ttc-fonts` / `google-noto-serif-cjk-ttc-fonts`・`poppler-utils`・
   `tesseract` / `tesseract-langpack-eng` / `tesseract-langpack-jpn` を入れる。#1611）。

   ```bash
   sudo apt-get install -y --no-install-recommends \
     libreoffice-core libreoffice-writer libreoffice-impress libreoffice-calc fonts-noto-cjk \
     poppler-utils tesseract-ocr tesseract-ocr-jpn
   ```

2. systemd が PID 1 の環境（WSL2 は `/etc/wsl.conf` の `[boot] systemd=true`）では、unit と sudoers を登録する（`rag/` で実行）。
   venv を作り（docling のモデルも取得する）、unit を `/etc/systemd/system/` に、
   画面から操作する unit の `systemctl` / `journalctl` だけを現在のユーザーに許可する sudoers を
   `/etc/sudoers.d/production-ready-rag-services` に置き、初めて登録した unit を起動する。

   ```bash
   scripts/rag-services.sh install            # 前処理 4 つと既定の CPU parser（docling）
   scripts/rag-services.sh install --unstructured  # Unstructured（Docling が扱えない形式を取り込む場合）
   scripts/rag-services.sh status             # unit の状態
   scripts/rag-services.sh render /tmp/units  # 登録せずに unit と sudoers の中身だけ確かめる
   scripts/rag-services.sh uninstall          # unit と sudoers を消す
   ```

   backend を `PLATFORM_ENVIRONMENT=local`（明示指定）で起動すると、サービス管理画面の起動 / 停止が有効になる。
   起動は `systemctl enable --now`、停止は `systemctl disable --now` なので、PC を再起動しても最後に操作した状態に戻る。
   `install` を再実行しても、停止したサービスは停止のまま（コードを更新したときは起動中のものだけ再起動する）。

3. systemd が無い環境では、使うサービスだけ前面で起動する（Ctrl+C で停止。サービス管理画面の起動 / 停止は使えないが、
   状態は /health で表示される）。

   ```bash
   scripts/rag-services.sh run parser-docling
   ```

   Docling / Unstructured の venv の torch / torchvision は、PyTorch の CPU 版の index（`https://download.pytorch.org/whl/cpu`）
   から入れる（各 `pyproject.toml` の `[[tool.uv.index]]` と `[tool.uv.sources]`。#1650）。PyPI の Linux の torch は CUDA 版で
   triton・`nvidia-*` / `cuda-*` の wheel（docling で約 2.7GB）を依存に持つが、配備先の Compute と CI の runner は GPU を
   持たないので、lock に入れない。本番の `init_script.sh`・開発の `rag-services.sh`・CI は同じ lock から
   `uv sync --locked` で入れる（以前の `RAG_SERVICES_TORCH=cpu` の置き換えは無くした）。

### 既定の解析エンジン（Docling）で扱える形式と、それ以外の形式の取り込み方（#286）

既定の文書解析エンジン **Docling**（`parser-docling`。見出し・表・図の領域を持つレイアウト解析）は **PDF と画像だけ**を解析する。

| 形式 | 既定（Docling）のまま | 取り込み方 |
|---|---|---|
| PDF（`.pdf`）・画像（`.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp` / `.gif`） | 解析できる | そのまま「処理を開始」 |
| Word・PowerPoint（`.docx` / `.pptx`） | 解析できない | 処理レシピの「ファイル準備」で **Office→PDF** を選ぶ（PDF にしてから Docling で解析）か、「文書解析」で **Unstructured** を選ぶ |
| Excel（`.xlsx`） | 解析できない | 処理レシピの「ファイル準備」で **Excel→JSONL** を選ぶ（成果物の JSONL は Docling のまま読める）か、「文書解析」で **Unstructured** を選ぶ。**Office→PDF は Excel を変換しない**（#2470） |
| 旧形式・マクロ付きの Office（`.doc` / `.ppt` / `.xls`・`.docm` / `.pptm` / `.xlsm`） | 原本のままでは解析しない | 処理レシピの「ファイル準備」で変換を選ぶ（Word・PowerPoint は **Office→PDF**、Word は **Word→JSONL** も可、Excel は **Excel→JSONL**）。マクロは実行しない（LibreOffice は変換ごとのプロファイルでマクロを無効にし、Excel→JSONL は VBA を読まない）。変換を選ばずに始めると、ファイル準備を飛ばして案内を出す（#2470） |
| ODT・RTF（`.odt` / `.rtf`） | 原本のままでは解析しない | 処理レシピの「ファイル準備」で **Office→PDF** を選ぶ（PDF にしてから解析する）。Unstructured は原本を読まない（pandoc を入れないため。#3351） |
| テキスト・Markdown・reStructuredText・Org・CSV / TSV・JSON / JSONL・XML・HTML（`.txt` / `.md` / `.rst` / `.org` / `.csv` / `.tsv` / `.json` / `.jsonl` / `.xml` / `.html` など） | 解析できない | 処理レシピの「文書解析」で **Unstructured** を選ぶ（`.rst` / `.org` はプレーンテキストとして読む。#3351） |
| メール（`.eml`） | 解析できない | 処理レシピの「文書解析」で **Unstructured** を選ぶ |
| TIFF・Outlook（`.msg`）・不明な形式 | どの解析エンジンでも取り込めない（以前から「非対応形式」として止まる） | PDF などに変換してからアップロードする |
| HEIC / HEIF の画像（`.heic` / `.heif`）・EPUB（`.epub`） | アップロードできない（画面の選択と API の 415 で、変換の方法を添えて止める） | 画像は JPEG / PNG（iPhone は「設定 › カメラ › フォーマット › 互換性優先」で JPEG で保存できる）、EPUB は PDF に変換してからアップロードする |
| 音声・動画（`.mp3` / `.wav` / `.m4a` / `.flac` / `.ogg` / `.aac`・`.mp4` / `.mov` / `.webm` など、`audio/*` / `video/*`） | アップロードできない（画面の選択と API の 415 で止める。#3369） | 内容をテキストに書き起こしてからアップロードする |

- **HEIC / HEIF・EPUB・ODT・RTF の原本を解析する依存は入れない（#3351）。** Unstructured の解析サービスは `unstructured[all-docs]`
  ではなく、残す形式の extra（`pdf,image,docx,pptx,xlsx,csv,tsv,md`）だけを入れ、`pi-heif`（同梱の libheif / libde265 が LGPL-3.0。
  HEVC の特許の懸念もある）と `pypandoc-binary`（同梱の pandoc が GPL-2.0+）を `exclude-dependencies` で外す。unstructured の
  PDF・画像の解析が import する `pi_heif` は、`rag_parser_core.unstructured_compat` が何もしない stub で満たす（HEIC の opener を
  登録しないだけ）。

- Docling で解析できない形式は、**取込を始める前に止まる**。アップロードの結果に「このままでは取込を開始できません」と
  理由・対処を出し、処理レシピの「処理を開始」（文書一覧の一括取込を含む）は HTTP 409 で理由・対処を返す。自動で
  Unstructured へ振り分けない。判定は backend の `app/rag/parser_source_guard.py` の 1 か所で、ファイル準備が
  passthrough 以外（Office→PDF など形式が変わる）のときは変換後の形式で取込時に判定する。
- 処理レシピで Unstructured を選んだ文書は止めない。Unstructured の解析サービス（`parser-unstructured`）は**既定では配備しない**。
  - 本番: Terraform の stack で入力 `rag_enable_parser_unstructured` を有効にして再配備し、「運用設定 › サービス管理」で起動する。
  - 開発: `scripts/rag-services.sh install parser-unstructured`（または `--unstructured`）で登録する。
  - サービスが未配備・停止中のときは、止めたときの案内にその旨を添える。
- 全体の既定を Unstructured にする場合は「取込・検索・回答設定 › 文書解析」で選んで保存する（`RAG_PARSER_ADAPTER_BACKEND=unstructured`）。
- **構造化レコードの JSONL**（ファイル準備の「CSV→JSONL」「Excel→JSONL」「Word→JSONL」が作る JSON Lines。
  `rag_parser_core.structured_records`。content type `application/vnd.production-ready.structured-records+jsonl`）は、
  選んだ解析エンジン（Docling・Unstructured）のサービスが外部の parser に渡さず共通の契約で読む（#2457・#21。専用の解析エンジンは
  削除した）。一般の `.json` / `.jsonl` は構造化レコードとみなさない（上の表のとおり Unstructured で読む）。
  記録を 1 要素にし、検索用テキストの記録と記録の間に独立した行の `<<<JSONL_RECORD_BOUNDARY>>>` を置く（原文の同じ文字列は
  `<<<` + k 個の `\` + `JSONL_RECORD_BOUNDARY>>>` を k+1 個にする可逆のエスケープで区別する）。分割方式「固定分割」
  （`fixed_delimiter`）は、分割符の設定に関係なくこの境界で 1 記録 1 chunk にし（chunk size / overlap で再分割・結合しない）、
  シート・行・セル範囲・列の役割・記録の ID を chunk に残す。chunk の本文・索引・引用にマーカーは残らない。契約に合わない JSONL・
  1 要素の上限（8,000 字）を超える記録・境界の数が記録の数と合わない抽出は、黙って切らずに取込を止める。

自前のコードの `docker-compose.yml` / `docker-compose.dev.yml` と Dockerfile は #356 で削除した。以前の Docker の環境からの移行は
「[既存環境の更新手順（#286）](#既存環境の更新手順286-docker-compose-からネイティブ配備への移行)」を参照する。

### ローカル保存ディレクトリ(`PLATFORM_LOCAL_STORAGE_DIR`)

`PLATFORM_UPLOAD_STORAGE_BACKEND=local` の原本は `PLATFORM_LOCAL_STORAGE_DIR/objects/` に保存される。既定値は製品ごとに `/u01/data/<製品名>` で統一している(RAG: `/u01/data/production-ready-rag`、NL2SQL: `/u01/data/production-ready-nl2sql`、Agent: `/u01/data/production-ready-agent`)。

#### 旧既定値 `/u01/production-ready-rag` からの移行

以前の既定値は `/u01/production-ready-rag` だった。backend は**ファイルを自動で移動しない**。

- 共通 `.env`（`platform/.env`）などで `PLATFORM_LOCAL_STORAGE_DIR` を明示設定している環境は、そのまま従来のディレクトリを使い続ける。
- 以前の Docker Compose の配備の named volume `backend-local-storage` の中身は、#286 の移行で `/u01/data/production-ready-rag` へ写す(`model-settings.json` は #211 以降 `platform/` に置く)。
- `PLATFORM_LOCAL_STORAGE_DIR` 未設定(既定値)で起動し、旧ディレクトリにデータが残っている場合は、起動時に警告ログ `legacy_local_storage_dir_detected` を出す。旧ディレクトリが新ディレクトリへの symlink になっていれば警告は出ない。

旧ディレクトリの原本を引き続き参照するには、次のいずれかを行う(NL2SQL の `init_script.sh` と同じく、コピー後に旧ディレクトリを退避して symlink を張る)。

```bash
# 1) 旧ディレクトリを使い続ける
#    platform/.env に PLATFORM_LOCAL_STORAGE_DIR=/u01/production-ready-rag を設定して再起動する

# 2) 新ディレクトリへ移行する(backend を停止してから実行)
OLD=/u01/production-ready-rag
NEW=/u01/data/production-ready-rag
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" "${NEW}"
cp -an "${OLD}/." "${NEW}/"   # -n: 新ディレクトリの既存ファイルは上書きしない
mv "${OLD}" "${OLD}.legacy-$(date +%Y%m%d%H%M%S)"
ln -s "${NEW}" "${OLD}"
```

移行後に backend を起動し、警告ログが出ないこと、既存文書のプレビュー / 再取込ができることを確認してから退避ディレクトリを削除する。
readiness の確認は `/api/ready` を使う。`oci_common`、`enterprise_ai`、`genai`、`oracle`、`object_storage` の設定グループを確認する。
`PLATFORM_ENVIRONMENT=production` では `audit_context_salt` も確認し、`RAG_AUDIT_CONTEXT_HASH_SALT` を必須にする。すべて `ok` のときだけ 200 になり、`missing`、`invalid`、`missing_credentials`、`wallet_not_found` が含まれる場合は 503 になる。

## 既存環境の更新手順（#21 構造化レコード JSONL・XML ツリー JSON の解析エンジンの削除）

XML / JSON の木を読む解析エンジン（XML ツリー JSON）と、構造化レコードの JSONL 専用の解析エンジン（構造化レコード JSONL）を
削除した（parser のサービス・設定・画面・サービス管理の行を外した）。残る解析エンジンは Docling（既定）・Unstructured。表の変更
（migration）は無い。

- ファイル準備の CSV→JSONL・Excel→JSONL・Word→JSONL は残る。その成果物（構造化レコードの JSONL）は、選んだ解析エンジン
  （Docling・Unstructured）のサービスが外部の parser に渡さず共通の契約（`rag_parser_core.structured_records`）で読む。
  記録の単位の要素・記録の境界・固定分割の 1 記録 1 chunk は今までどおり。取込の前に「構造化レコード JSONL を選ぶ」案内で
  止めることも無くなった。処理レシピでファイル準備にこの 3 つを選んでも、解析エンジンは書き換えない（分割の上書きだけを入れる）。
- `parser-settings.json`・処理レシピ・KB 構築設定・取込ジョブに削除したエンジン（`structured_records_jsonl`・`xml_tree_json`）と
  その有効化の項目が残っていても、読み込みで既定（全体は Docling、レシピは全体の継承）へ寄せる（次の保存で消える。#22 と同じ仕組み）。
  `backend/.env` の `RAG_PARSER_ADAPTER_BACKEND` に削除したエンジンを書いていた環境も、起動時に既定の `docling` へ寄せる。
- `backend/.env` の `RAG_PARSER_STRUCTURED_RECORDS_JSONL_*`・`RAG_PARSER_XML_TREE_JSON_*`・`RAG_XML_TREE_JSON_SOURCE_URL_TEMPLATE` は
  読まない（残っていても害は無いが、消してよい）。
- 抽出の ID（`extraction_recipe_id`）の入力の解析エンジン・前処理の成果物の形式の版（`structured_records_v1`）が変わるため、
  CSV / Excel / Word の前処理で取り込んだ文書は再処理で作り直しになる（再処理まで、それまでの active な chunk_set を検索に使う）。
- MCP の `rag_read_section` の引数 `citation`（XML ツリー JSON の引用ラベルで読む）と、質問の中の引用ラベルで段落を先頭に置く検索、
  XML ツリー JSON の段落の親子・参照先の内容を検索の文に足す処理は削除した。
- 以前の配備で入れた `production-ready-rag-parser-structured-records-jsonl.service`・`production-ready-rag-parser-xml-tree-json.service` は、
  再配備（`sudo -n ./platform/deploy/suite-update.sh`）で止めて（`disable --now`）消し、sudoers からも外す（`rag_services.txt` に
  残っていても読み飛ばして書き戻す）。開発は次を実行する。

  ```bash
  sudo systemctl disable --now production-ready-rag-parser-structured-records-jsonl.service production-ready-rag-parser-xml-tree-json.service
  sudo rm -f /etc/systemd/system/production-ready-rag-parser-structured-records-jsonl.service \
    /etc/systemd/system/production-ready-rag-parser-xml-tree-json.service
  sudo systemctl daemon-reload
  scripts/rag-services.sh install            # rag/ で実行。sudoers を今の unit だけで作り直す
  ```

  使っていなかった venv（`services/parsers/structured_records_jsonl/.venv`・`services/parsers/xml_tree_json/.venv`）が残っていれば消してよい。
- XML ツリー JSON で取り込んだ文書の索引はそのまま検索できる。解析し直すときは、文書の詳細で Unstructured などの使えるエンジンを
  選んで再処理する。

## 既存環境の更新手順（#22 MinerU・Dots.OCR・OCI Document Understanding の解析の削除）

外部サービス・OCI に依存する解析エンジン MinerU・Dots.OCR・OCI Document Understanding を削除した
（client・parser のサービス・設定・画面・ライセンスの確認を外した）。残る解析エンジンは Docling（既定）・
Unstructured（構造化レコード JSONL・XML ツリー JSON は #21 で削除）。表の変更（migration）は無い。

- 使用エンジンの並びは Docling → Unstructured（処理レシピの選択肢も同じ）。「外部 GPU 解析エンジンの接続」の節と「Dots.OCR の読み取りプロンプト」は無くなった。
- `parser-settings.json`・処理レシピ・取込ジョブに削除したエンジンが残っていても、読み込みで既定
  （全体は Docling、レシピは全体の継承）へ寄せる（次の保存で消える）。
- `backend/.env` の `RAG_PARSER_MINERU_*` / `RAG_PARSER_DOTS_OCR_*` / `RAG_OCI_DOCUMENT_UNDERSTANDING_*` と
  `RAG_PARSER_OCI_DOCUMENT_UNDERSTANDING_SERVICE_URL` は読まない（残っていても害は無いが、消してよい）。
  `RAG_PARSER_ADAPTER_BACKEND` に削除したエンジンを書いていた環境は、起動時に既定の `docling` へ寄せる。
- 以前の配備で入れた `production-ready-rag-parser-oci-document-understanding.service` は、再配備
  （`sudo -n ./platform/deploy/suite-update.sh`）で止めて（`disable --now`）消し、sudoers からも外す。
- 削除したエンジンで取り込んだ文書の索引はそのまま検索できる。解析し直すときは、文書の詳細で
  Docling などの使えるエンジンを選んで再処理する。

## 既存環境の更新手順（#3343 PDF の処理を PyMuPDF から pypdfium2 に置き換え）

backend の PDF の処理（文書のプレビューの頁の画像・bbox の切り出し・頁の境目の段落の続きの判定の
行の位置・Docling の文字の照合）を、PyMuPDF（AGPL）から PDFium（pypdfium2。BSD-3-Clause / Apache-2.0）と Pillow に置き換えた
（`app/rag/pdf_backend.py`）。設定・DB・保存値の変更は無い。

- **依存**: backend の依存から `pymupdf` を外した。backend の venv を lock で同期し直す（`uv sync --locked --no-dev`。配備の
  更新の手順が行う）と消える。pypdfium2 は既存の依存で、OS の package も増えない。書体を埋め込まない PDF は OS の書体
  （`google-noto-sans-cjk-ttc-fonts` など）で描く。
- **並行**: PDFium はスレッド安全ではないため、backend の process の中の PDFium の呼び出しは 1 つの lock で直列にする（#3362）。
  頁の描画は直列になり、PNG の符号化は lock の外で行う。
- **振る舞いの変化**: 合字（ﬁ など）は分けた文字として読む。1 字ずつ縦に積んだ字は縦の行として扱い、横書きの行の位置の判定に
  使わない。WebP の画像の原本も切り出せる。頁の画像の PNG は以前より 2〜3 割大きい。暗号化・破損した PDF は以前と同じく 422。

## 既存環境の更新手順（#3377 親子階層の分割をどの解析の結果でも行う）

文書分割の親子階層（`small_to_big`。既定）を、Docling のレイアウトの記録（`layout_records`）を持つ文書に限らず、どの解析の結果でも行うようにした（[rag-engine.md の「使い方」2.](./rag-engine.md)）。これまでは、それ以外の文書（標準の解析の Markdown・テキスト、Unstructured・OCI などの解析の結果、構造化レコード JSONL の CSV・Word など）を構造認識（`structure_aware`）へ縮退していた（#300）。

- 変わるもの: 親子階層を選んだ、レイアウトの記録・Excel の行の記録・XML ツリー JSON 以外の文書の分割。子は構造認識と同じ要素の分割を子の目標文字数（`RAG_CHUNK_CHILD_TARGET_CHARS`。表は `RAG_CHUNK_TABLE_CHILD_TARGET_CHARS`）で行い、親は節をまたがずに `RAG_CHUNK_PARENT_TARGET_CHARS` / `RAG_CHUNK_PARENT_MAX_CHILDREN` / `RAG_CHUNK_PARENT_MAX_PAGES` の中でまとめる。構造化レコード JSONL は 1 記録 = 1 子。新しい chunk には `chunk_strategy_fallback_reason` を付けない。取込の trace は `chunk_parent_source` に親子を作った入力を残す。
- 変わらないもの: Docling のレイアウトの文書（Office→PDF を含む）・Excel の行の記録（1 行 = 1 子。#1349）・XML ツリー JSON（#2024）の親子、親子階層以外の分割方式（`fixed_delimiter` の 1 記録 = 1 chunk を含む）。
- 構造認識などの本文の分割で、文の途中の chunk の境目に収まらなかった文だけの小さな chunk を作らず、次の chunk の先頭にするようにした（同じ本文の chunk の数と境目が変わることがある）。

schema の変更・migration・DDL は無い。解析の結果（抽出）は変わらないので、再解析は要らない。

1. backend を更新して再起動すると、それ以降に作る Chunk から新しい分割になる。
2. 既存の chunk は自動では作り直さない（今の chunk のまま検索の対象に残る）。以前に縮退して作った chunk（`chunk_strategy_fallback_reason=layout_missing`）は、文書詳細の Chunk タブに「親子階層で分割されていない Chunk です」と表示する。親子階層にするには、文書の詳細のレシピで Chunk の工程から処理し直す（抽出は使い回す）。chunk が変わるので embedding をやり直す（OCI Generative AI の利用が増える）。

## 既存環境の更新手順（#3369 音声・動画の取込の削除）

音声のファイル（`.mp3` / `.wav` / `.m4a` / `.flac` / `.ogg` / `.aac`）の取込（backend が OCI AI Speech の transcription job で
文字起こしする経路）を削除した。#2042 は GPU の ASR の解析エンジン（`parser-asr`）だけを外し、OCI AI Speech の経路が残っていた。
音声・動画は製品として対応しない形式と決めた（利用者の判断。2026-10-26）。音声・動画のファイルは、アップロードの画面（送る前）と
API（415）で「音声・動画のファイル（MP3・WAV・OGG・MP4 など）は対応していません。…」と止める。表の変更（migration）は無い。
抽出の ID（`extraction_recipe_id`）は変わらない（削除した設定の既定値を hash の入力に固定で残す）ので、ほかの文書の再処理は要らない。

- 未リリースのため互換は持たない。`backend/.env` の `RAG_PARSER_ASR_ENABLED`・`RAG_OCI_SPEECH_*`（compartment・namespace・
  入力 / 出力の bucket・prefix・言語・poll の間隔・timeout）は読まない（残っていても害は無い）。
- 取り込み済みの音声の文書（文字起こしで索引を作ったもの）は検索に残るが、原本は未知の形式（`unknown_file_type`）として扱われ、
  再処理できない。

1. `backend/.env` から `RAG_PARSER_ASR_ENABLED`・`RAG_OCI_SPEECH_*` を消す（任意）。
2. 音声の文書が残っていれば、文書インデックスから削除する（内容をテキストに書き起こしたファイルを取り込み直す）。
3. tenancy に OCI AI Speech のための IAM policy（`ai-service-speech-family` など）を足していれば、RAG では使わなくなったので外してよい。

## 既存環境の更新手順（#3358 取込時の「PIIマスク」の削除）

ファイル準備の変換方式「PIIマスク」（`pii_redact`。サービス `preprocess-pii-redact`、18016、Presidio と spaCy の日本語モデル
`ja_core_news_lg`）を削除した。配備はこのサービスの venv に spaCy のモデルを取得しなくなる（`uv sync --inexact` の例外も
外した）。回答の安全チェック（OCI の個人情報の検出）は変わらない。後方互換は持たない。

- **設定**: `RAG_PREPROCESS_PII_REDACT_SERVICE_URL` は読まない（`backend/.env` に残っていても無視する。消してよい）。
  `RAG_PREPROCESS_PROFILE=pii_redact` や、文書レシピ・KB 構築設定の `preprocess_profile` に `pii_redact` が残っていれば、
  読み替えずに検証エラーになる。更新の前に別の変換方式（多くは「変換なし」）を選んで保存し直す。PIIマスクで取り込んだ文書の
  chunk は今のまま（再処理すると、選び直した方式で取り込み直す）。
- **データベース**: DDL・データの移行は要らない。
- **本番（OCI Compute）**: `sudo platform/deploy/suite-update.sh` だけでよい。`rag/init_script.sh` は以前の stack が書いた
  `/u01/aipoc/props/rag_services.txt` の `preprocess-pii-redact` を読み飛ばして一覧を書き戻し、その unit を止めて
  （`disable --now`）消し、sudoers も今の unit だけで作り直す。
- **開発（`scripts/rag-services.sh install` で unit を入れた環境）**: `install` は今のサービスの unit しか探さないため、手で消す。

  ```bash
  sudo systemctl disable --now production-ready-rag-preprocess-pii-redact.service
  sudo rm -f /etc/systemd/system/production-ready-rag-preprocess-pii-redact.service
  sudo systemctl daemon-reload
  scripts/rag-services.sh install            # rag/ で実行。sudoers を今の unit で作り直す
  ```

  使っていなかった venv（`services/preprocess/pii_redact/.venv`。spaCy のモデルを含む）が残っていれば消してよい。

## 既存環境の更新手順（#3347 ファイル準備「PDF を画像として読み直す」の削除）

ファイル準備の変換方式「PDF を画像として読み直す」（`pdf_to_page_images`。サービス `preprocess-pdf-to-page-images`、18011、
PyMuPDF）を削除した。スキャンの PDF は既定の解析エンジン Docling が OCR（RapidOCR）で読む。ページの単位で検索したいときは、
分割方式「ページ単位」（`page_level`）を使う。後方互換は持たない。

- **設定**: `RAG_PREPROCESS_PDF_TO_PAGE_IMAGES_SERVICE_URL` は読まない（`backend/.env` に残っていても無視する。消してよい）。
  `RAG_PREPROCESS_PROFILE=pdf_to_page_images` や、文書レシピ・KB 構築設定の `preprocess_profile` に `pdf_to_page_images`
  が残っていれば、読み替えずに検証エラーになる。更新の前に別の変換方式（多くは「変換なし」）を選んで保存し直す。
- **データベース**: DDL・データの移行は要らない。
- **本番（OCI Compute）**: `sudo platform/deploy/suite-update.sh` だけでよい。`rag/init_script.sh` は以前の stack が書いた
  `/u01/aipoc/props/rag_services.txt` の `preprocess-pdf-to-page-images` を読み飛ばして一覧を書き戻し、その unit を止めて
  （`disable --now`）消し、sudoers も今の unit だけで作り直す。
- **開発（`scripts/rag-services.sh install` で unit を入れた環境）**: `install` は今のサービスの unit しか探さないため、手で消す。

  ```bash
  sudo systemctl disable --now production-ready-rag-preprocess-pdf-to-page-images.service
  sudo rm -f /etc/systemd/system/production-ready-rag-preprocess-pdf-to-page-images.service
  sudo systemctl daemon-reload
  scripts/rag-services.sh install            # rag/ で実行。sudoers を今の unit で作り直す
  ```

  使っていなかった venv（`services/preprocess/pdf_to_page_images/.venv`）が残っていれば消してよい。

## 既存環境の更新手順（#3226 処理の経過を共通の span の表に書く）

処理の経過（チャット・RAG 検索・実行の詳細）の span を、3 製品共通の span の表（`PLATFORM_TRACE_SPANS`）に書いて読む
ようにした。表はシステムテーブルの初期化が作る（共通の DDL。**共用の ADB に作る前に利用者の確認が要る**）。

1. システムテーブルを更新する（`system_schema_cli initialize`。新しい表と索引だけで、データを消さない）。更新するまで、
   span の書き込みは失敗してログ（`trace_span_write_failed`）に出し、処理の経過は空になる（回答・検索は止まらない）。
2. backend と frontend を同時に更新して再起動する（回答の配信の SSE の `chat_progress` と、段階の polling / SSE
   （`.../messages/{回答の id}/progress[/stream]`）を外した。画面は記録ごとの trace の API を読み直す）。
3. 更新の前に保存した回答・実行の処理の経過（段のイベントの一覧）は読まない。その記録は「古い形式の記録のため、処理の
   経過を表示できません。」を出す（回答・引用は変わらない）。
4. 保存期限は共通の `PLATFORM_TRACE_RETENTION_DAYS`（既定 0 = 無期限。量の目安は `platform/.env.example`）。

## 既存環境の更新手順（#3235 実行の記録の既定の保存期間を無期限にする）

回答の記録（`rag_answer_records`）・チャットと RAG 検索の実行履歴（`rag_run_history`）・品質評価の job（`rag_evaluation_jobs`）は、既定で無期限に保存し、自動では削除しない。

- **環境変数**: `RAG_ANSWER_RECORD_RETENTION_DAYS` の既定は `0`（無期限）。明示した正の値がある環境はその値で期限切れを削除するため、無期限にするときは `0` にする。画面には保存期間の編集を出さない。
- **品質評価の job**: 終わった job を 7 日で消す処理をやめた（設定は無い）。
- **データベース**: DDL・データの移行は要らない。以前に削除した記録は戻らない。
- **手順**: コードを更新して backend を再起動する（`sudo platform/deploy/suite-update.sh`）。

## 既存環境の更新手順（#3218 pipeline の段のサービスへの委譲を削除）

検索インデックス・文書構造索引の構築・安全チェック・評価の基準の段は、backend の中で実行する（`rag_pipeline_core` の決定論の解決）。段のサービス（`services/pipeline/{vector_index,graphrag,guardrail,evaluation}`）と、そこへ HTTP で委譲する仕組み（`app/clients/pipeline_stage.py`）を削除し、サービス管理の一覧からも外した。これらのサービスは配備の対象ではなく（systemd の unit も無い）、委譲の許可の既定が有効だったため、Chunk 作成の job ごとに接続に失敗して WARNING「pipeline stage service unavailable; falling back to in-process」を出していた。

- **環境変数**: 次は読まない（`backend/.env` に残っていても無視する。消さなくてもよい）。
  - `RAG_VECTOR_INDEX_SERVICE_ENABLED`・`RAG_VECTOR_INDEX_SERVICE_URL`・`RAG_GRAPH_SERVICE_ENABLED`・`RAG_GRAPH_SERVICE_URL`・`RAG_GUARDRAIL_SERVICE_ENABLED`・`RAG_GUARDRAIL_SERVICE_URL`・`RAG_EVALUATION_SERVICE_ENABLED`・`RAG_EVALUATION_SERVICE_URL`・`RAG_PIPELINE_STAGE_TIMEOUT_SECONDS`
- **サービス管理の API**: `GET /api/services/catalog` の各サービスから `deployable` を削除し、`systemd_unit` は常に unit 名を返す。稼働状態の `in_process` と、使用方針の `in_process_when_disabled`・`required_no_fallback` は返さない（どのサービスも使っていない）。
- **データベース**: DDL・データの移行は要らない。
- **手順**: コードを更新して backend・取込 worker・frontend を再起動する（`sudo platform/deploy/suite-update.sh`）。Compute で段のサービスを手で起動していた場合は止めてよい。

## 既存環境の更新手順（#2831 引用証拠のキーの改名）

回答の引用一覧は `cited_evidence`、その ID 一覧は `cited_evidence_ids`、引用一覧の各 ID は `evidence_id` に変更した。旧名の `used_images` / `used_image_ids` / 引用一覧内の `image_id` は実行時には受け付けない。SDK の呼び出し側も新名へ更新する。実際の画像のメタデータ `image_evidence[*].image_id` は変更しない。

DB の保存済み回答は、通常のシステムテーブル更新で migration `20261022_001_cited_evidence` を適用してから新しい backend を使用する。`RAG_ANSWER_RECORDS.EVALUATION_INPUT_JSON` の引用キーだけを改名し、引用順序、`confirmed` / `reference`、画像メタデータ、回答本文を保持する。新旧キーの重複がある場合は上書きせず停止するため、重複内容を確認して解消してから再実行する。再適用しても移行済みの値は変わらない。

SDK / CLI が書き出した回答 JSON は DB の migration の対象外である。対象の回答 JSON ファイル、または回答 artifact だけを置いたディレクトリを指定して、まず変更件数を確認し、その後に保存する。元ファイルは既存のバックアップ手順で退避する。repo の直下で実行する例:

```bash
cd rag/backend
uv run python -m rag_engine.knowledge.answer_migration /path/to/answer.json
uv run python -m rag_engine.knowledge.answer_migration /path/to/answer.json --apply
```

このコマンドも引用キーだけを変更する。引用根拠のメタデータ内にある同名の項目には触れない。新旧キーが重複しているファイルは保存せずエラーにする。複数ファイルは一つずつ保存するので、途中で停止した場合は問題のファイルを確認し、同じコマンドを再実行する。

## 既存環境の更新手順の共通の注意（データを削除する migration。#619）

システムテーブルの migration には、テーブルの DROP や行の DELETE で**データを削除するもの**がある（例: #596 の `20260930_005_retire_standard_engine_objects`）。削除したデータは元に戻せないため、次のようにしている。

- **更新は承認が無ければ止まる。** 未適用の migration にデータを削除するものがあると、`system_schema_cli initialize`・画面の「システム設定 > データベース > RAG システムテーブル」の「作成・更新」・`init_script.sh` の再実行は、DB を何も変えずに止まる（error code `SCHEMA_DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED`）。一覧と、削除されるデータ・前にすることの説明は `system_schema_cli status` の `pending_destructive_migrations` と、画面の警告に出る。削除しない migration だけが未適用のときは、今までどおり承認なしで更新する。未初期化の DB の初期作成（migration を実行せずに記録だけする）と、確認語で承認する全再作成も対象外。
- **テストは当てない。** 実 Oracle のテスト（`tests/conftest.py` の `oracle_db`）は、テストの開始時に未適用の migration を当てるが、データを削除する migration が未適用なら何も当てずに実 Oracle のテストを skip し、理由を出す。実 Oracle のテストは、共有の開発 DB ではなくテスト専用の schema で流すことを推奨する（`rag/AGENTS.md` の「テスト/検証方針」）。

データを削除する migration を当てる手順:

1. `system_schema_cli status` の `pending_destructive_migrations` で、削除されるデータを確かめる。
2. 残すデータを書き出す（各更新手順の書き出し。例: #596 は `app.rag.legacy_export`）。ADB の自動バックアップが有効で、保持期間の中にあることも確かめる（OCI コンソールの Autonomous AI Database の「バックアップ」）。手元の Oracle（コンテナなど）はバックアップが無いので、必要なら先に Data Pump などで書き出す。
3. 承認して更新する。CLI は `--allow-destructive` を付ける。画面は「作成・更新」の確認ダイアログで「削除して更新」を押す。

   ```bash
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.system_schema_cli initialize --allow-destructive
   ```

`init_script.sh` は承認を付けずに `initialize` を実行する。データを削除する migration が未適用なら、WARNING を出して配備を続ける（backend は起動する）ので、上の手順で書き出してから適用する。

### 書き出す前に消えたデータを、DB のバックアップから取り出す

削除した表は `PURGE` のため recyclebin にも無く、`FLASHBACK TABLE` では戻せない。ADB（Oracle Autonomous AI Database）なら、自動バックアップから過去の時点の**別の DB（クローン）**を作り、そこから必要な表だけを取り出す。元の DB をその時点へ戻す復元（in-place のリストア）は、3 製品が共有する schema 全体とその後の更新を巻き戻すので使わない。

1. 消えた時刻を確かめる。migration の適用時刻は `SELECT migration_name, applied_at FROM rag_schema_migrations WHERE migration_name = '<migration の名前>'` で分かる。
2. OCI コンソールで対象の Autonomous AI Database を開き、「クローンの作成」で「バックアップからのクローン」→「特定の時点」を選び、1. の時刻の少し前を指定する（自動バックアップの保持期間の中の時点だけ選べる）。クローンは別の料金がかかる。
3. クローンの Wallet を取得し、クローンに接続して表を書き出す。RAG の書き出しの CLI は接続先を環境変数で変えられる（例: クローン用の共通 `.env` を用意し、`PLATFORM_ENV_FILE=<そのファイル> uv run python -m app.rag.legacy_export --table rag_agent_memories --output <出力先>`）。必要な行を元の DB へ戻す場合は、戻す先の表（無くなった表は戻さない）と取り込み方を決めてから行う。
4. 取り出しが終わったらクローンを終了（削除）する。

## 既存環境の更新手順（#2839 メタデータ/項目抽出を既定で無効にする）

「メタデータ/項目抽出」（`RAG_FIELD_EXTRACTION_ENABLED`）の既定を無効（`false`）にした（#537 から既定で有効だった）。

- `backend/.env` で値を明示していない環境は、更新後の次の取込から項目を抽出しない（文書ごとの OCI Enterprise AI の
  呼び出しが減る）。既に抽出した値は消えない。
- 続けて使うときは、「取込・検索・回答設定 › 文書解析」の「解析後の処理」で「メタデータ/項目抽出」を有効にして保存する
  （`backend/.env` に `RAG_FIELD_EXTRACTION_ENABLED=true` を書く）。文書のレシピで文書ごとに有効にすることもできる。
- 値を明示している環境は何も変わらない。データの移行（migration）は不要。

## 既存環境の更新手順（#2457 ファイル準備 Word→JSONL の追加）

ファイル準備に Word→JSONL（`word_to_jsonl`。サービス `preprocess-word-to-jsonl`、unit `production-ready-rag-preprocess-word-to-jsonl.service`、
port 18017、URL の設定 `RAG_PREPROCESS_WORD_TO_JSONL_SERVICE_URL`）を足した。DB の表・列・migration は増えない（保存値の移行も無い）。

1. 本番（OCI Compute）: `sudo platform/deploy/suite-update.sh` で再配備する。`init_script.sh` が、以前の stack の `rag_services.txt` に
   無くても必ず配備するサービスとして足し、`libreoffice-core`・`libreoffice-writer` を入れて unit を作って起動する。
2. 開発（このマシンの worktree・主の checkout）: `scripts/rag-services.sh install preprocess-word-to-jsonl`（LibreOffice が無ければ
   注意を出す。.docx・.docm は LibreOffice なしで読める）。backend の `.env` は既定（`127.0.0.1:18017`）なら書かなくてよい。
3. backend・取込 worker を再起動する。「運用設定 › サービス管理」に Word→JSONL が出て、起動していることを確かめる。

## 既存環境の更新手順（#2457 ファイル準備 CSV→JSONL・Excel→JSONL への改名と保存値の移行）

ファイル準備の `csv_to_json` / `excel_to_json` を `csv_to_jsonl` / `excel_to_jsonl` に改名した（旧名は読まない）。サービス・systemd の
unit・ディレクトリ・URL の設定も改名した: `preprocess-csv-to-json` → `preprocess-csv-to-jsonl`（`services/preprocess/csv_to_jsonl`）、
`preprocess-excel-to-json` → `preprocess-excel-to-jsonl`（`services/preprocess/excel_to_jsonl`）、
`RAG_PREPROCESS_CSV_TO_JSON_SERVICE_URL` → `RAG_PREPROCESS_CSV_TO_JSONL_SERVICE_URL`、
`RAG_PREPROCESS_EXCEL_TO_JSON_SERVICE_URL` → `RAG_PREPROCESS_EXCEL_TO_JSONL_SERVICE_URL`（port は 18012・18013 のまま）。
あわせて次を変えた。

- Docling / Unstructured は構造化レコードの JSONL を受けない。CSV→JSONL・Excel→JSONL と組み合わせると、取込を始める前（と解析の
  サービス）で「構造化レコード JSONL を選ぶ」診断を出して止める（黙って変換しない）。（#21 で専用の解析エンジンを削除し、
  Docling / Unstructured のサービスが共通の契約で読むように変えた。この節の解析エンジンの書き換えは、読み込みで既定へ寄る。）
- 固定分割符の既定を `\n\n` から `<<<JSONL_RECORD_BOUNDARY>>>` にした（global の初期値・API・画面・プレビュー・実処理）。
  文書レシピも分割符（`chunk_delimiter`）を持てるようになり、画面でファイル準備に CSV→JSONL・Excel→JSONL を選ぶと、解析エンジン
  「構造化レコード JSONL」と分割「固定分割符」・境界のマーカーを見える上書きとして入れる。固定分割以外の方式の chunk_set の ID は変わらない。

保存値は次のとおり移す（分割の方式は書き換えない）。

| 保存の場所 | 対象 | 書き換え |
|---|---|---|
| DB（migration `20261019_003_structured_records_jsonl_profiles`） | `rag_document_recipes.processing_config`・`rag_documents.processing_config`・`rag_knowledge_bases.retrieval_config`・`rag_ingestion_jobs.settings_overrides`・`rag_chunk_sets.recipe_subset`・`rag_document_extractions.recipe_subset` | `"csv_to_json"` / `"excel_to_json"`（と抽出の ID の入力の `*_format` の key）を `*_jsonl` に |
| DB（同じ migration。文書レシピの 2 列だけ） | ファイル準備が `*_jsonl` で解析エンジンが Docling / Unstructured / 未指定（global を継承）のレシピ | 解析エンジンを `structured_records_jsonl` に |
| DB（同じ migration。文書レシピの 2 列だけ） | 分割方式が固定分割で分割符が未指定のレシピ | 分割符に旧い既定の `\n\n` を明示 |
| `backend/.env`・`parser-settings.json`（`app.rag.structured_records_settings_migration`） | 上の 2 つの URL の変数・`RAG_PREPROCESS_PROFILE`・global の解析エンジン・`RAG_CHUNKING_STRATEGY=fixed_delimiter` で `RAG_CHUNK_DELIMITER` が無い設定 | 改名・`structured_records_jsonl` へ・`RAG_CHUNK_DELIMITER=\n\n` を明示 |

migration は値の書き換えだけで、データを消さない（承認は要らない。何度当てても同じ結果）。改名の前のコードは移した値（`csv_to_jsonl`・
レシピの `chunk_delimiter`）を読めないため、**同じ DB を使う環境は、このコードへの更新と migration を同時に行う**。
抽出の ID が変わるので、CSV / Excel の文書は再処理で作り直しになる（再処理まで、それまでの active な chunk_set を検索に使う）。

1. 本番（OCI Compute）: `sudo platform/deploy/suite-update.sh` で再配備する。`init_script.sh` が
   - `rag_services.txt` の旧名（`preprocess-csv-to-json`・`preprocess-excel-to-json`）を新しい名前として読み、`rag_services.txt` を
     新しい名前で書き戻す（#2484）。
   - CSV→JSONL・Excel→JSONL は必ず配備するサービス（`REQUIRED_RAG_SERVICES`）なので、#2484 の前の再配備で `rag_services.txt` から
     消えた環境（サービス管理画面で「未登録」）も、`suite-update.sh` で再配備すれば unit と sudoers が入り、既定どおり起動する（#2486）。
   - 新しい unit と sudoers を作り、旧い unit（`production-ready-rag-preprocess-csv-to-json.service`・
     `production-ready-rag-preprocess-excel-to-json.service`）を「配備しなくなった unit」として止めて消す。新しい unit は旧い unit の
     状態を引き継ぐ（旧い unit が enable なら起動し、利用者が停止して disable なら停止のまま。旧い unit が無ければ既定どおり起動する）。
   - `app.rag.structured_records_settings_migration --apply` で `backend/.env`・`parser-settings.json` を移し、
     `system_schema_cli initialize` で上の migration を当てる。
   - 旧いディレクトリの venv（`rag/services/preprocess/csv_to_json/.venv` など。git の管理外）は残るので、要らなければ消す。
2. 開発（このマシンの worktree・主の checkout）:
   ```bash
   sudo systemctl disable --now production-ready-rag-preprocess-csv-to-json.service production-ready-rag-preprocess-excel-to-json.service
   sudo rm -f /etc/systemd/system/production-ready-rag-preprocess-csv-to-json.service /etc/systemd/system/production-ready-rag-preprocess-excel-to-json.service
   sudo systemctl daemon-reload
   rm -rf services/preprocess/csv_to_json services/preprocess/excel_to_json   # 旧いディレクトリに残った venv
   scripts/rag-services.sh install preprocess-csv-to-jsonl preprocess-excel-to-jsonl
   cd backend
   uv run python -m app.rag.structured_records_settings_migration            # 書き換えの予定を出す
   uv run python -m app.rag.structured_records_settings_migration --apply    # backend/.env・parser-settings.json を移す
   uv run python -m app.rag.system_schema_cli initialize                       # migration を当てる
   ```
   `scripts/rag-services.sh install` は sudoers を作り直し、旧い unit の許可を消す。
3. backend・取込 worker を再起動する。
4. 確かめる: 文書レシピに旧名が残っていないこと（`SELECT COUNT(*) FROM rag_document_recipes WHERE JSON_SERIALIZE(processing_config)
   LIKE '%_to_json"%'` が 0）と、CSV→JSONL・Excel→JSONL のレシピの解析エンジンが「構造化レコード JSONL」になっていること。
   global の分割符を `\n\n` 以外の値で保存していて、固定分割を選んだレシピがその値を継承していた場合は、移行で `\n\n` が明示される。
   そのレシピの「固定分割符文字列」を保存していた値に直してから再処理する。

## 既存環境の更新手順（#2452 文書の削除の後始末の再試行）

文書の削除で保存先の原本・成果物を消せなかったとき（保存先の一時的な障害など）、消す対象（原本の URI・成果物の URI・
成果物の prefix）を文書の行の削除と同じ transaction で表 `rag_document_cleanups` に残し、後から消し直すようにした。
記録には保存先の参照だけを持ち、原本の内容・認証情報は持たない。すべて消せたら記録の行を消す。

- 取込ワーカーが、取込の固着の回復と同じ間隔（`RAG_INGESTION_QUEUE_RECOVERY_INTERVAL_SECONDS`）で、最後に試してから
  5 分以上たった記録を 1 回に 20 件まで消し直す。
- 同じ文書の削除（`DELETE /api/documents/{document_id}`）を実行すると、文書の行が無くても記録が残っていればすぐに消し直す
  （削除した利用者の記録だけ。system_admin は全員の分）。記録も無ければ今までどおり 404。
- 消せたものは記録から外す。無いファイルは消せたものとして扱う。残っている文書（tenant を問わない）が参照する URI と、
  同じ ID の文書が残っているときの文書の prefix は消さない。

1. システムテーブルを更新する（migration `20261019_002_document_cleanups`）。表 `rag_document_cleanups` と索引を無いときだけ
   作る（データは変えない。承認は要らない）。表が無い間は文書の削除が失敗する。
2. backend・取込 worker を再起動する。
3. 残っている記録は `SELECT document_id, attempt_count, last_error_type, updated_at FROM rag_document_cleanups` で確かめる
   （`last_error_type` は最後の失敗の例外の型。`attempt_count` が増え続けるときは保存先の権限・到達性を確かめる）。

## 既存環境の更新手順（#2450 取り消した migration を当てた DB の文書の分類の列を戻す）

#2127（PR #2130）の migration `20261016_002_document_effective_period` は #2131 で取り消したが、取り消す前に当てた
環境がある。その DB は `rag_documents.classification` が `effective_period` に改名され（有効期間以外の key は消えた）、
`rag_query_history.classification_filter` が無い。台帳には適用済みで残るため、今のコードの「文書の分類」の一覧
（`GET /api/document-categories`）・文書の分類の保存・検索の分類の条件・検索の履歴が `ORA-00904` で失敗する。

- 追加の migration `20261019_001_restore_document_classification` が、無い列（`rag_documents.classification`・
  `rag_query_history.classification_filter`。どちらも JSON）を足す。`effective_period` があれば、`classification` が
  NULL の行にだけ有効期間（`effective_from` / `effective_to`）を写す。写すのは YYYY-MM-DD の日付で開始日 < 終了日の
  値だけで、形の合わない値は写さない（`effective_period` に残る）。列の改名・削除・行の削除はしないので、承認は要らない。
  新しい DB・この migration を当てていない DB では何もしない（何度当てても同じ）。
- **戻らないもの**: 取り消した migration が消した分類（大分類・中分類・小分類・推定元など）と、検索の履歴の分類の
  条件は DB に残っていない。要るなら、その migration の適用より前の書き出しか、ADB のバックアップから取り出す
  （上の「書き出す前に消えたデータを、DB のバックアップから取り出す」。適用の時刻は台帳の `applied_at`）。
- `effective_period` の列は消さずに残す（今のコードは読まない）。写した値を確かめた後に消すかは、利用者が決める。
- システムテーブルの状態（`system_schema_cli status`・画面の「RAG システムテーブル」）は、台帳が適用済みでも
  正本の列が欠けた表があれば「更新必要」（`outdated`）にし、欠けた列を `missing_columns` に出す。

1. 当てた環境か確かめる（どれも読むだけ）。

   ```sql
   SELECT migration_name, applied_at FROM rag_schema_migrations
   WHERE migration_name = '20261016_002_document_effective_period';
   SELECT table_name, column_name FROM user_tab_columns
   WHERE (table_name = 'RAG_DOCUMENTS' AND column_name IN ('CLASSIFICATION', 'EFFECTIVE_PERIOD'))
      OR (table_name = 'RAG_QUERY_HISTORY' AND column_name = 'CLASSIFICATION_FILTER');
   ```

2. 更新の前に、対象の値を書き出す（データは消えないが、写した結果と比べるため）。例:
   `SELECT document_id, effective_period FROM rag_documents WHERE effective_period IS NOT NULL` の結果を保存する。
   ADB の自動バックアップが保持期間の中にあることも確かめる。
3. backend を更新し、システムテーブルを更新する（`system_schema_cli initialize`・画面の「作成・更新」・
   `init_script.sh` の再実行のどれか）。
4. 結果を確かめる。`system_schema_cli status` が `ready` で `missing_columns` が空になり、「ナレッジ構築 > 文書の分類」
   が開ける。写さなかった値は
   `SELECT document_id, effective_period FROM rag_documents WHERE effective_period IS NOT NULL AND classification IS NULL`
   で分かるので、要るなら文書の詳細で有効期間を入れ直す。

## 既存環境の更新手順（#2395 業務ガイドの手順ごとの検索と参照の頁）

業務ガイドの資料に頁の範囲（`references[].page_ranges`）を、手順に参照（`steps[].references`）を足し、内容の形の版
（`schema_version`）を 2 にした。版 1 の内容・取り込みの JSON は受け付けない（取り込む JSON の `schema_version` を 2 にする）。
回答では、照合した業務ガイドの手順（分岐で残った手順）ごとに「質問 + 手順の検索の手がかり」で検索し、手順の参照の頁に絞った
検索の根拠を順位に関係なく回答の材料に入れる。

1. システムテーブルを更新する（migration `20261018_004_support_guide_schema_v2`）。保存済みの下書きと公開の版の
   `schema_version` を 1 から 2 に書き換える（ほかの値は変えない。データは消えない。承認は要らない）。
2. 設定（`rag/backend/.env`。どれも任意で既定のままでよい）: `RAG_GUIDE_STEP_RETRIEVAL_ENABLED`（既定 true）・
   `RAG_GUIDE_STEP_MAX_QUERIES`（4）・`RAG_GUIDE_STEP_CHUNKS_PER_STEP`（2）・`RAG_GUIDE_REFERENCE_MAX_CHUNKS`（4）・
   `RAG_GUIDE_STEP_DEADLINE_SECONDS`（60。#2826）と、追加の検索（手順ごとの検索・小問ごとの検索・根拠の無い要求の再検索）の回数の合計の枠
   `RAG_EXTRA_RETRIEVAL_MAX_QUERIES`（8）。手順ごとの検索の ON / OFF と回数・時間の上限は全体設定だけで決め、検索・回答プロファイルでは上書きしない（上限は #3523 から）。
3. backend を再起動する。

## 既存環境の更新手順（#2847 文書分割を backend 内で実行）

すべての文書分割方式を backend 内で直接実行する。分割用の HTTP 委譲、専用サービスと HTTP 契約を削除し、
サービス管理画面から文書分割のサービス項目を除いた。分割方式・分割結果・保存済み chunk は変更しない。
backend と取込 worker を更新して再起動する。旧 `RAG_CHUNKING_SERVICE_ENABLED` / `RAG_CHUNKING_SERVICE_URL` は
使わないため、設定ファイルに残っていれば削除する。独自に分割のサービスを起動していた環境では、そのプロセスも停止する。

## 既存環境の更新手順（#2850 根拠の無い要求の再検索の既定）

「根拠の無い要求を探し直す」の既定を無効にした。`RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED` を
指定していない環境は、更新後の backend・取込 worker の再起動から無効になる。
既存の環境変数に明示した値は維持する。保存済みの有効値も無効にしたい場合は、
取込・検索・回答設定 › 検索方法の「回答の検索と生成」でこの項目を無効にして保存する
（この項目は全体設定だけで決め、検索・回答プロファイルに保存された同名の値は #3053 から適用しない）。

## 既存環境の更新手順（#2277 見出しの階層の推定（AI））

任意の取込の段「見出しの階層の推定（AI）」を足した。既定は無効で、既定のままなら取込・抽出結果・抽出の使い回しの鍵は
変わらない。API の項目（文書解析の設定の `heading_level_inference_enabled`・文書レシピの同じ名前の項目）が増えた。表の変更は無い。

- 対象: しおりの深さを当てた見出しが無く、見出しが `RAG_HEADING_LEVEL_INFERENCE_MIN_HEADINGS`（既定 3）以上
  `RAG_HEADING_LEVEL_INFERENCE_MAX_HEADINGS`（既定 300）以下で、番号の体系（「1.」「1.2」「第 1 章」など）で深さを
  決められない見出しの割合が `RAG_HEADING_LEVEL_INFERENCE_UNRESOLVED_RATIO`（既定 0.5）以上の、Docling の解析結果
  （要素の `section_path` を `layout_records` から作る結果）。ほかの解析エンジンの結果には当てない。
- 段ごとの解析をまとめ、文書全体の整理（#2001）を当てた後の原本全体に 1 回だけ当てる（章節の木・項目抽出・chunk より前）。
  見出しの一覧を一括で送り、通常のモデル呼び出しは対象文書ごとに 1 回。一時的な通信障害や流量制限などでは
  共通の実行器が再試行するため、実際の呼び出しは複数回になる場合がある。本文全体を送ったり、見出しを分割して送ったりはしない。
- OCI Enterprise AI には見出しの文字列だけ（読み順。1 つ `RAG_HEADING_LEVEL_INFERENCE_MAX_TITLE_CHARS` 文字まで）を
  JSON で渡し、構造化出力の `{"levels":[{"i":番号,"level":1〜6}]}` を受け取る。応答は Pydantic で厳密に確かめ（余分な
  key・欠け・重複・範囲外は不可）、合わない・呼び出しの失敗のときは今の階層のまま取込を続ける。
- 受け取った階層は見出しの record の `raw.heading_level`（`raw.heading_source=llm_heading_level`）に入れ、要素の
  `section_path` を作り直す（章節の木と chunk の `section_path` が同じ階層になる）。結果は抽出結果の
  `parser_artifacts.heading_level_inference`（`status`: `applied` / `skipped` / `invalid_response` / `failed` と理由）。
- プロンプトは回答プロンプトの画面の「各段のプロンプト」の「見出しの階層を推定する」で見られる。
- 有効にすると抽出の使い回しの鍵が変わるため、対象の文書は解析し直す（取込を再実行する）。

1. 使うときは、文書解析の画面の「解析後の処理」で「見出しの階層の推定（AI）」を有効にして保存する（`backend/.env` の
   `RAG_HEADING_LEVEL_INFERENCE_ENABLED`）か、文書のレシピで上書きする。閾値を変えるときは `backend/.env` に
   `RAG_HEADING_LEVEL_INFERENCE_*`（雛形 `rag/backend/.env.example`）を書き、backend・取込 worker を再起動する。

## 既存環境の更新手順（#2273 ページの境目の段落の続きの判定に行の位置を足す）

ページの境目で切れた段落の続きの判定（文書全体の整理。#2001。Docling の段の境目）を次のように変えた。表の続きの
判定・API・表の変更は無い。

- 前のページの末尾・次のページの先頭の図・表・caption・脚注・ページの定型を読み飛ばして、段落の続きの元・続きを探す
  （ページ下端の図・表の後ろに続く段落もつなぐ）。見出し・数式などほかの record で探すのをやめる。
- 文の終わりの規則（言語資源）に加えて、PDF の文字の層の行の位置で確かめる。
  - 前の段落が 2 行以上なら、最後の行の右端が段落の右端まで届く（差が行の高さ × `RAG_PAGE_CONTINUATION_RIGHT_EDGE_TOLERANCE`
    未満。既定 1.0）。
  - 次の段落が 2 行以上なら、最初の行に字下げが無い（差が行の高さ × `RAG_PAGE_CONTINUATION_INDENT_TOLERANCE` 未満。既定 0.5）。
  - 両方が 2 行以上なら、幅が近い（差が狭いほうの幅 × `RAG_PAGE_CONTINUATION_WIDTH_DIFFERENCE_RATIO` 未満。既定 1.0）。
  - 文字の層の無いページ（スキャン）・縦書き・回転したページ・行の取れない record は、今までどおり文の終わりの規則だけで
    判定する。`RAG_PAGE_CONTINUATION_GEOMETRY_ENABLED=false` で行の位置の確認を切れる。
  - 行の位置で続きにしなかった数は、抽出結果の `parser_artifacts.document_consolidation.continuations_rejected_by_geometry`。
- 同じページの中の段・図をまたぐ続き（段組みの段の境目）は扱わない（Docling の読み順への依存が強く、誤りの影響が大きいため）。
- 既に取り込んだ文書の抽出結果・chunk は変わらない（再取込して解析し直したときに変わる）。設定の値は抽出結果の使い回しの鍵に
  含めないため、値を変えたら対象の文書を解析し直す。

1. 必要なら `rag/backend/.env` に `RAG_PAGE_CONTINUATION_*`（雛形 `rag/backend/.env.example`）を設定し、backend・取込 worker を
   再起動する（既定のままなら設定は要らない）。

## 既存環境の更新手順（#2131 文書の分類の一覧の管理）

#2127（PR #2130）で削除した文書の分類（大分類・中分類・小分類）を戻し、分類の一覧（大分類 → 中分類 → 小分類）を
管理する画面「ナレッジ構築 > 文書の分類」を足した。文書の詳細と RAG 検索の分類の欄は、この一覧から選ぶ（どれも任意）。
#2130 のデータを削除する migration（`20261016_002_document_effective_period`）は取り消した（今のコードは当てない）。
**取り消す前にこの migration を当てた環境がある。** 台帳（`rag_schema_migrations`）に記録があれば、下の
「#2450 取り消した migration を当てた DB の文書の分類の列を戻す」の手順で列を戻す。

1. システムテーブルを更新する（migration `20261016_003_document_categories`）。表 `rag_document_categories` と索引を
   無いときだけ作る（データは変えない。承認は要らない）。
2. 分類の一覧は空で始まる。「文書の分類」の画面で大分類・中分類・小分類を作る。今の文書の分類の値は変わらない
   （一覧に無い値も、文書の詳細では今の値として残る）。
3. 「文書の分類」の画面を使うロールに、権限管理でメニューの権限「文書の分類」（`menu.document_categories`）を付ける。
   文書の詳細・RAG 検索で分類を選ぶだけなら、その画面の権限（アップロード・文書インデックス・RAG 検索）で足りる。

## 既存環境の更新手順（#2099 品質評価の設定比較の名残の列の削除）

#2088 で品質評価の設定比較を削除した後も DB に残っていた、設定比較だけが使った列・索引と job の種類を消した。画面・API の
挙動は変わらない（#2088 の後のコードはどれも読み書きしていない）。

1. システムテーブルを更新する（migration `20261016_001_retire_evaluation_compare`）。設定比較の job の行
   （`rag_evaluation_jobs.kind = 'compare'`）を削除し、`kind` の CHECK を `'run'` だけにし、列
   `rag_evaluation_jobs.current_experiment_id`・`rag_evaluation_runs.best_experiment_id` と索引
   `rag_evaluation_runs_best_experiment_idx` を削除する（あるときだけ。何度当てても同じ）。**データを削除する migration
   なので、承認（CLI の `--allow-destructive`・画面の確認ダイアログ）が要る**（上の「既存環境の更新手順の共通の注意」）。
   評価の結果（`rag_evaluation_runs` の行と `result_json`）は残る。設定比較の job の記録が要るときだけ、更新の前に
   `rag_evaluation_jobs` を書き出す。
2. backend の更新との順序は問わない（更新の前のコードも、消す列・値を使っていない）。

## 既存環境の更新手順（#2055 関係情報を回答の検索に使う）

#2055 で、文書構造索引の構築（`RAG_GRAPH_PROFILE=entities`）で作った章節・表 / 図の見出しの entity と chunk の対応を、回答の検索の候補を広げるのにも使えるようにした。既定は OFF で、更新しただけでは回答は変わらない。

- **schema の変更は無い。** 既存の表（`rag_graph_entities`・`rag_graph_entity_chunks`）を読むだけで、migration は要らない。
- **新しい設定**: `RAG_GRAPH_EXPANSION_ENABLED`（既定 `false`。検索方法の画面の「文書構造による検索拡張」が書く）と `RAG_GRAPH_EXPANSION_MAX_CHUNKS`（既定 `6`。1〜20）。どちらも全体設定だけで決め、検索・回答プロファイルでは上書きしない（`RAG_GRAPH_EXPANSION_MAX_CHUNKS` は #3523 から。保存済みのプロファイルの `graph_expansion_max_chunks` は適用せず、次の保存で消える）。
- **関係情報の無い文書には効かない。** 文書構造索引の構築を「構築しない」で取り込んだ文書は、文書の詳細の処理レシピで「文書構造索引の構築」を「構築する」にして「再処理」するまで、何も足さない。

## 既存環境の更新手順（#2042 OCI Vision / ASR の parser とファイル準備の 2 つの変換方式の削除）

OCI Generative AI（Vision）の parser（`parser-oci-genai-vision`、18027）と GPU の ASR の parser（`parser-asr`、18026）を削除した（音声の文字起こしは
その後 backend の OCI AI Speech で行っていたが、#3369 で音声・動画の取込ごと削除した。取込の「図・画像を AI で読み取る」
（OCI Enterprise AI の VLM）は変わらない）。
「ファイル準備」の変換方式のうち、スキャン画像の補正（`preprocess-image-enhance`、18015）と URL→Markdown
（`preprocess-url-to-markdown`、18014）も削除した。保存済みの設定・文書レシピにこの 2 つが残っていても、読み込み時に
「変換なし」（passthrough）として扱い、次に保存すると消える。DB の migration は無い。
（このときに使用エンジンにした XML ツリー JSON の解析は #21 で削除した。）

1. 本番（OCI Compute）: `sudo platform/deploy/suite-update.sh` だけでよい（stack の再適用は要らない）。`rag/init_script.sh` は
   必ず配備する parser（`parser-docling`）を、以前の stack が書いた `/u01/aipoc/props/rag_services.txt`
   に無くても足し、提供しなくなったサービス（`parser-oci-genai-vision`・`parser-asr`・`preprocess-image-enhance`・`preprocess-url-to-markdown` など）は読み飛ばして、
   `rag_services.txt` を今の一覧に書き戻す（所有者と mode は保つ。書けなくても配備は続ける）。削除したサービスの
   unit は止めて（`disable --now`）消し、sudoers も今の unit だけで作り直す。`/var/log/rag-init.log` に
   `Adding required RAG service ...`・`Skipping RAG service that is no longer provided: ...`・
   `Removing systemd unit that is no longer deployed: ...` が出る。
2. 開発: 登録済みの削除したサービスの unit を止めて消し、`scripts/rag-services.sh install` で
   sudoers を作り直す（`install` は今のサービスの unit しか探さないため、削除したサービスの unit は手で消す）。

   ```bash
   sudo systemctl disable --now production-ready-rag-parser-asr.service production-ready-rag-parser-oci-genai-vision.service \
     production-ready-rag-preprocess-image-enhance.service production-ready-rag-preprocess-url-to-markdown.service
   sudo rm -f /etc/systemd/system/production-ready-rag-parser-asr.service \
     /etc/systemd/system/production-ready-rag-parser-oci-genai-vision.service \
     /etc/systemd/system/production-ready-rag-preprocess-image-enhance.service \
     /etc/systemd/system/production-ready-rag-preprocess-url-to-markdown.service
   sudo systemctl daemon-reload
   scripts/rag-services.sh install            # rag/ で実行
   ```

   使っていなかった venv（`services/parsers/asr/.venv`・`services/parsers/oci_genai_vision/.venv`・
   `services/preprocess/image_enhance/.venv`・`services/preprocess/url_to_markdown/.venv`）が残っていれば消してよい。
3. backend・取込 worker を再起動する。

## 既存環境の更新手順（#1990 新しい環境の検索の索引を既定の「高精度」の値で作る）

#1990 で、システムテーブルの作成（新しい環境の初期作成・全再作成）が作る主検索の HNSW 索引 `rag_chunks_embedding_hnsw_idx` を、既定の検索精度「高精度」（`accurate`）の推奨ビルド（`TARGET ACCURACY 98` / `NEIGHBORS 48` / `EFCONSTRUCTION 800`）にした。以前は「バランス」の値（95 / 32 / 500）で作っていたため、既定のまま「取込・検索・回答設定 > 検索インデックス」に「索引の再作成が必要です」と出ていた。

- **既存の環境の DB は変わらない。** backend・`init_script.sh` の再実行・システムテーブルの更新（`system_schema_cli initialize`・画面の「作成・更新」）は既存の索引を作り直さない（無い索引だけを作る）。`system_schema_cli status` の状態も変わらず、更新は要らない（migration の checksum は版で固定した SQL から作り、索引は名前で判定する）。
- **既存の環境は、次のどちらかにする。** 何もしなければ、既定の「高精度」で「索引の再作成が必要です」と出たまま検索は動く（検索時の target accuracy は 98）。
  1. 「検索インデックス」の画面の再作成の SQL を DBA が適用する（`DROP INDEX` → `CREATE VECTOR INDEX`。作り直す間は近似検索が索引を使えず遅くなるので、取込と検索の少ない時間に行う。Vector Pool の見積もりは [oracle-hnsw-vector-index.md](./oracle-hnsw-vector-index.md) の「Vector Pool メモリ設計」）。
  2. 検索精度「バランス」を選ぶ（今の索引と同じ値で、再作成は要らない。検索時の target accuracy は `RAG_ORACLE_VECTOR_TARGET_ACCURACY`（既定 95））。

## 既存環境の更新手順（#635 RAG 検索の検索・回答プロファイルを 1 つにする）

- **画面**: RAG 検索の「対象の検索・回答プロファイル」を 1 つだけ選ぶ欄にし、チャットの検索・回答プロファイルの欄と同じ部品・文言・幅にした。以前の画面で複数を選んでいた作業状態（sessionStorage の `search.searchAnswerProfileIds`）は読まず、再読込後は未選択から始まる。
- **API**: `POST /api/search`・`POST /api/search/stream` は検索・回答プロファイルを `search_answer_profile_id`（1 つ）だけで受ける。`search_answer_profile_ids` を送ると 422 を返す（読み捨てると検索・回答プロファイルの外を検索するため）。検索の絞り込みの候補 `GET /api/search/extraction-fields` の query も `search_answer_profile_ids` から `search_answer_profile_id` に変えた。MCP の `rag_search`（当時あったチャットの送信のツールは #787 で削除）と検証 CLI（`app.rag.answer_verify_cli`）は元から 1 つで、入力は変わらない。
- **データベース**: DDL・データの移行は要らない。回答の記録・質問の履歴は元から検索・回答プロファイルを 1 つだけ保存している。以前に複数の検索・回答プロファイルで検索した監査（`rag_search_audit` の `search_answer_profile_applied`）は `bv-1,bv-2` の文字列のまま残り、表示・集計は変わらない。
- **手順**: コードを更新して backend と frontend を再起動する。`/api/search` を直接呼ぶ独自のスクリプトがあれば、`search_answer_profile_ids: [id]` を `search_answer_profile_id: id` に変える。

## 既存環境の更新手順（#599 rag_poc から移したときの名前の改名）

#599 で、rag_poc から移したときの名前（`docrag`）を、コードの識別子・設定（環境変数）・API・DB の名前から外し、RAG の標準の名前にした。回答・分割の挙動は変わらない。旧名との互換は持たない（旧名の環境変数は読まず、保存値と表は migration で書き換える）。#599 の migration はデータを削除しないので、承認（`--allow-destructive`）は要らない（#621 の `20260930_007_retire_graph_claims_community` などデータを削除する migration が未適用なら、そちらの承認が要る。下の「既存環境の更新手順（#621）」と上の「共通の注意」）。

### 名前の対照

**backend の環境変数**（`backend/.env`。取込・検索・回答設定の画面から保存した値もここにある）。旧名の行は読まないので、改名しないと既定値に戻る。

| 旧名 | 新しい名前 |
|---|---|
| `RAG_DOCRAG_QUERY_STRATEGY` | `RAG_QUERY_STRATEGY` |
| `RAG_DOCRAG_ANSWER_FLOW` | `RAG_ANSWER_FLOW` |
| `RAG_DOCRAG_NEIGHBOR_CHILD_COUNT` | `RAG_NEIGHBOR_CHILD_COUNT` |
| `RAG_DOCRAG_RERANK_ENABLED` | `RAG_RERANK_ENABLED` |
| `RAG_DOCRAG_SCREEN_LINKING_ENABLED` | `RAG_SCREEN_LINKING_ENABLED` |
| `RAG_DOCRAG_ANSWER_VISION_ENABLED` | `RAG_ANSWER_VISION_ENABLED` |
| `RAG_DOCRAG_HISTORY_REWRITE_ENABLED` | `RAG_HISTORY_REWRITE_ENABLED` |
| `RAG_DOCRAG_CHILD_TARGET_CHARS` | `RAG_CHUNK_CHILD_TARGET_CHARS` |
| `RAG_DOCRAG_TABLE_CHILD_TARGET_CHARS` | `RAG_CHUNK_TABLE_CHILD_TARGET_CHARS` |
| `RAG_DOCRAG_PARENT_TARGET_CHARS` | `RAG_CHUNK_PARENT_TARGET_CHARS` |
| `RAG_DOCRAG_PARENT_MAX_PAGES` | `RAG_CHUNK_PARENT_MAX_PAGES` |
| `RAG_DOCRAG_PARENT_MAX_CHILDREN` | `RAG_CHUNK_PARENT_MAX_CHILDREN` |

**process の環境変数**（`rag_engine` の package が読む。`backend/.env` ではなく、systemd の unit の `Environment=` か shell の `export` で渡すもの）: `DOCRAG_*` は `RAG_ENGINE_*` にした（docling サービスの `DOCRAG_RENDER_DPI` → `RAG_ENGINE_RENDER_DPI`・`DOCRAG_OUTPUT_DIR` → `RAG_ENGINE_OUTPUT_DIR`）。

**コード・package**: `rag/packages/docrag_core`（distribution `docrag-core`、import `docrag`）は `rag/packages/rag_engine`（distribution `rag-engine`、import `rag_engine`）。backend の `app/rag/docrag_answer.py` → `answer_engine.py`、`docrag_chunking.py` → `chunking_small_to_big.py`、`docrag_prompts.py` → `answer_prompts.py`、`docrag_verify_cli.py` → `answer_verify_cli.py`（`python -m app.rag.answer_verify_cli`）。CI の job `RAG / DocRAG core` は `RAG / Engine`。

**保存値・API**:

| 対象 | 旧名 | 新しい名前 |
|---|---|---|
| 分割方式（`RAG_CHUNKING_STRATEGY`・文書 / レシピの処理設定・KB の構築設定） | `docrag_small_to_big` | `small_to_big` |
| 分割のパラメータ（処理設定・文書分割の設定の API） | `docrag_child_target_chars` など 5 項目 | `chunk_child_target_chars` など（`docrag_` を `chunk_` に） |
| 検索・回答プロファイルの検索・回答設定（`profile_config.query`） | `docrag_query_strategy`・`docrag_answer_flow`・`docrag_neighbor_child_count`・`docrag_rerank_enabled`・`docrag_screen_linking_enabled` | `query_strategy`・`answer_flow`・`neighbor_child_count`・`rerank_enabled`・`screen_linking_enabled` |
| 回答生成のプロンプトの表・API | `rag_docrag_prompts`・`/api/settings/docrag-prompts` | `rag_answer_prompts`・`/api/settings/answer-prompts` |
| 検索の応答の `diagnostics` | `retrieval_strategy: "docrag"`・`retrieval_strategy_adapter: "docrag_grounded"` / `"docrag_retrieval_only"`・`docrag`（回答フローの診断） | `"hybrid"`・`"grounded"` / `"retrieval_only"`・`answer` |
| 回答の記録の詳細の API・チャットの SSE の `metadata` | `docrag` | `answer_diagnostics` |
| 回答の記録の回答の方式（`rag_answer_records.answer_engine`） | `docrag` | `grounded` |
| 進捗の stage・metrics の `stage` label・監査の `error_stage` | `docrag_answer`・`docrag_history_rewrite` | `answer`・`history_rewrite` |
| 親子階層の chunk の metadata | `docrag_parent_text`・`docrag_metadata_json`・`docrag_search_text`・`docrag_chunk_id`・`docrag_chunk_seq`・`docrag_source_record_refs_json`・`docrag_source_seq_ranges_json`・`docrag_first_page_context_json`、`source_parser: docling_docrag`、`chunk_group_kind: docrag_parent` | `parent_text`・`engine_metadata_json`・`engine_search_text`・`engine_chunk_id`・`engine_chunk_seq`・`source_record_refs_json`・`source_seq_ranges_json`・`first_page_context_json`、`docling_layout`、`small_to_big_parent` |
| Docling の解析結果（`parser_artifacts`）・分割の縮退の理由 | `docrag_layout`・`docrag_layout_missing` | `layout_records`・`layout_missing` |
| 派生情報レイヤーの指紋の入力 | `docrag_chunk_contract` | `chunk_metadata_contract` |

`chunk_set_id` は改名の前の名前で hash するので変わらない（既存の Chunk・embedding をそのまま使う。作り直しは要らない）。検索の設定の fingerprint（`config_fingerprint`）は入力の名前が変わるので、更新の前と後で値が変わる。Prometheus のダッシュボード・アラートで `stage="docrag_answer"` などを使っている場合は、新しい名前に直す。

### システムテーブルの更新がすること

- migration `20260930_008_answer_prompts_table`: 回答生成のプロンプトの表を `rag_docrag_prompts` から `rag_answer_prompts` へ移す。新しい表が無ければ `ALTER TABLE ... RENAME TO` で改名し、ある（システムテーブルの更新は migration の前に新しい表を作る）ときは旧表の行を新しい表へ写す。写した後、旧表は退役したオブジェクトとして消える（行は新しい表に残る）。主キーの制約と index はシステムの名前（`SYS_C...`）なので改名しない。
- migration `20260930_009_stored_engine_names`: 上の表の保存値を書き換える。対象は、文書・レシピの処理設定と解析結果、KB の構築設定、検索・回答プロファイルの設定、取込ジョブの上書き、chunk set・抽出・派生情報レイヤーの記録、chunk の metadata、回答の記録、会話・フィードバックの引用、品質評価の入力と結果の JSON の列と、回答の記録の `answer_engine`・検索の監査の `error_stage`。旧名を含む行だけを更新し（冪等）、行は消さない。chunk が多い環境（`rag_chunks` の親子階層の chunk はすべて更新する）では時間がかかり、undo を使う。
- migration `20261001_001_document_sections`: 人が修正した文書の章節（章節ナビゲーション）の表 `rag_document_sections` を足す（#713）。文書ごとに 1 行で、文書を消すと一緒に消える。表を足すだけで既存の行は変えない。修正の後に新しく抽出された章節は表示のときに自動で足し（「新しく抽出」の印。保存すると外れる）、人が消した抽出の章節は `sections_json` の `removed_source_ids` に覚えて足し直さない（#721。保存の形は `{"sections": [...], "removed_source_ids": [...]}`、以前の配列の形もそのまま読む）。チャットの確認で選んだ章節は、回答のときに章節 ID で今のページを読み直す。
- 章節の抽出規則（#715）: 全体の既定は `backend/section-rules.json`（`RAG_SECTION_RULES_FILE` で場所を変えられる。抽出項目の定義と同じ形）。ファイルが無ければ「解析エンジンの見出し」（これまでどおり）。規則は章節を表示するたびに保存済みの抽出結果へ当てるので、取込のやり直しも migration も要らない。処理レシピの「章節の抽出規則」で文書ごとに方式を上書きできる。

### 手順

1. #599 の版のコードを取得する（`git pull` など）。backend・取込 worker・前処理 / parser は止める（旧 package の venv のままでは起動できない。保存値の書き換えの最中に取込が書き込まないようにする）。

   ```bash
   sudo systemctl stop 'production-ready-rag-*'
   ```

2. `backend/.env` の旧名の行を改名し、値の旧名（`RAG_CHUNKING_STRATEGY=docrag_small_to_big`）も書き換える（バックアップを取ってから編集する。値が旧名のままだと backend が起動時の検証で止まる。#624）。Compute では、`init_script.sh` が Resource Manager の入力（`/u01/aipoc/props/backend.env`）から `backend/.env` を作り直すので、両方を直す。

   ```bash
   for env_file in /u01/aipoc/no.1-production-ready-suite/rag/backend/.env /u01/aipoc/props/backend.env; do
     [ -f "${env_file}" ] || continue
     sudo cp -p "${env_file}" "${env_file}.bak-599"
     sudo sed -i -E \
       -e 's/^RAG_DOCRAG_(CHILD_TARGET_CHARS|TABLE_CHILD_TARGET_CHARS|PARENT_TARGET_CHARS|PARENT_MAX_PAGES|PARENT_MAX_CHILDREN)=/RAG_CHUNK_\1=/' \
       -e '/^RAG_DOCRAG_PROFILE=/d' \
       -e 's/^RAG_DOCRAG_(QUERY_STRATEGY|ANSWER_FLOW|NEIGHBOR_CHILD_COUNT|RERANK_ENABLED|SCREEN_LINKING_ENABLED|ANSWER_VISION_ENABLED|HISTORY_REWRITE_ENABLED)=/RAG_\1=/' \
       -e 's/^RAG_CHUNKING_STRATEGY=docrag_small_to_big$/RAG_CHUNKING_STRATEGY=small_to_big/' \
       "${env_file}"
     sudo grep -ni 'docrag' "${env_file}" || echo "OK: ${env_file}"
   done
   ```

   ローカルは `rag/backend/.env` だけを同じ `sed` で直す。
3. process の環境変数 `DOCRAG_*` を `RAG_ENGINE_*` に直す。systemd の unit の drop-in や、起動する shell の `export` を探して改名する。ただし外部 DomainProfile の設定は廃止しているので、後述の #3668 の手順で除く。docling サービスの unit の `Environment=RAG_ENGINE_OUTPUT_DIR=...` は `scripts/rag-systemd.sh` が書くので、手順 4 の `init_script.sh` の再実行（ローカルは `scripts/rag-services.sh install`）で作り直す。

   ```bash
   sudo grep -rn 'DOCRAG_' /etc/systemd/system/production-ready-rag-* 2>/dev/null
   ```

4. venv を作り直す（package の名前が変わったので、旧 `docrag-core` を外して `rag-engine` を入れる）。Compute では `init_script.sh` を再実行すれば、venv・unit・frontend の build・システムテーブルの更新（手順 5）までを行う。手で行う場合は、リポジトリの所有者で実行する。

   ```bash
   # OCI Compute（手で行う場合）
   cd /u01/aipoc/no.1-production-ready-suite/rag
   sudo -u opc bash -c 'cd backend && uv sync --locked --no-dev --python 3.12'
   sudo -u opc bash -c 'cd services/parsers/docling && uv sync --locked --no-dev --python 3.12'

   # ローカル
   cd rag/backend && uv sync --locked
   cd ../services/parsers/docling && uv sync --locked
   ```

5. システムテーブルを更新する（#599 の migration だけが未適用なら承認は要らない）。画面の「システム設定 > データベース > RAG システムテーブル」の「作成・更新」でもよい。

   ```bash
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.system_schema_cli initialize
   ```

6. backend・worker・前処理 / parser と frontend（`npm run build` の成果物）を起動し直す（`sudo systemctl start 'production-ready-rag-*'`。`init_script.sh` を再実行した場合は起動済み）。
7. 確かめる。
   - `system_schema_cli status` が `ready` で、`retired_objects` と `pending_versions` が空。
   - 保存値に旧名が残っていない（0 件）。

     ```sql
     SELECT COUNT(*) FROM rag_chunks WHERE JSON_SERIALIZE(metadata_json RETURNING CLOB) LIKE '%docrag%';
     SELECT COUNT(*) FROM rag_search_answer_profiles WHERE JSON_SERIALIZE(profile_config RETURNING CLOB) LIKE '%docrag%';
     SELECT COUNT(*) FROM rag_answer_prompts;  -- 編集したプロンプトの件数が更新の前と同じ
     ```

   - 取込・検索・回答設定の「検索方法」「文書分割」と検索・回答プロファイルの「検索・回答設定」に、更新の前の値が出る。RAG 検索で回答し、「回答の根拠と実行記録」が出る。

## 既存環境の更新手順（#621 文書構造索引の構築の選択肢を 2 つにする）

#621 で、設定の「文書構造索引の構築」の選択肢を「構築しない」（`off`。既定）と「構築する」（`entities`）の 2 つにした。構築するのは、文書全体と章・節の見出し（表・図を含む）の「含む」のつながりで、LLM は使わない。ナレッジベースの「文書構造グラフ」で見るためのもので、回答の検索には使わない（#595）。

- **`full` を削除した。** `full` が追加で作っていた claims（`rag_graph_claims`）と community summary（`rag_graph_community_summaries`）は、どの画面・API・検索も読んでいなかった。`full` と `entities` が作る文書構造グラフの中身（`rag_graph_entities`・`rag_graph_relationships`・`rag_graph_entity_chunks`）は同じ。
- **legacy の `RAG_GRAPH_ENABLED` を削除した。** 読まない（`true` でも `full` に読み替えない）。設定画面の保存も書かない。
- **backend/.env の `RAG_GRAPH_PROFILE=full` は起動を止める。** 読み替えず、書き換え先（`entities` か `off`）を示すエラーで止まる。
- **保存済みの上書きの `full` は、migration `20260930_006_graph_profile_entities` が `entities` に書き換える**（文書・文書レシピの `processing_config`、ナレッジベースの `retrieval_config.ingestion`、取込ジョブの `settings_overrides.processing_config`、chunk_set に刻んだレシピ `recipe_subset`）。データを削除しない migration。
- **migration `20260930_007_retire_graph_claims_community` が `rag_graph_claims`・`rag_graph_community_summaries` を削除する。** データを削除する migration なので、承認（CLI の `--allow-destructive`・画面の確認ダイアログ）が要る（上の「既存環境の更新手順の共通の注意」）。どちらも文書から作り直せる派生データで、読む経路が無かったため、書き出しは要らない。
- **旧値との互換は持たない（#629）。** 保存値に `full` が残っていると、ほかの不正な値と同じ扱いになる（文書・文書レシピの処理設定などは読み込みが検証エラーになり、ナレッジベースの構築設定は警告を出して空の設定（すべて既定を継承）に戻る）。旧 `full` で構築した文書に残る `rag_graph_claims` の行も消さないので、表が残ったままだと再取込・削除が FK で失敗する。**新しい版の backend と取込 worker を起動する前に、2 つの migration（006・007）を当てる。** 2 つは同時に未適用になり、007 の承認が無いとどちらも当たらない。

### 手順

1. #621 の版のコードを取得する（`git pull` など）。backend・取込 worker・前処理 / parser は止める（migration の前に新しい版が保存値 `full` を読むと検証エラーになり、取込が関係情報の表に書き込まないようにする）。

   ```bash
   sudo systemctl stop 'production-ready-rag-*'
   ```

2. `backend/.env` に `RAG_GRAPH_PROFILE=full` があれば `RAG_GRAPH_PROFILE=entities` に書き換える（構築しない場合は `off`。`full` のままだと起動時の検証で止まる）。`RAG_GRAPH_ENABLED` の行があれば消す（残っても読まないが、`true` だった環境は、以前は `full` で構築していた。同じものを作り続けるなら `RAG_GRAPH_PROFILE=entities` にする）。Compute では、`init_script.sh` が Resource Manager の入力（`/u01/aipoc/props/backend.env`）から `backend/.env` を作り直すので、両方を直す。
3. backend の venv を新しい版に合わせる（リポジトリの所有者で実行する。`init_script.sh` は、システムテーブルの更新の後の手順 5 で再実行する）。

   ```bash
   # OCI Compute
   cd /u01/aipoc/no.1-production-ready-suite/rag
   sudo -u opc bash -c 'cd backend && uv sync --locked --no-dev --python 3.12'

   # ローカル
   cd rag/backend && uv sync --locked
   ```

4. サービスを止めたまま、システムテーブルを承認付きで更新する（006 と 007 を当てる）。

   ```bash
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.system_schema_cli initialize --allow-destructive
   ```

   画面の「システム設定 > データベース > RAG システムテーブル」の「作成・更新」の確認ダイアログ（「削除して更新」）で行う場合は、backend だけを起動し（取込 worker と前処理 / parser は止めたまま）、ほかの操作をせずに更新する。当てるまでは、保存値に `full` が残る設定が上のとおり読めない（この間にナレッジベースの構築設定を画面から保存すると、既定に戻った設定のまま保存される）。
5. backend・worker・前処理 / parser と frontend を起動し直す（`sudo systemctl start 'production-ready-rag-*'`。Compute では `init_script.sh` の再実行でもよい。migration は当たっているので、承認なしの更新で止まらない）。
6. 確かめる。
   - `system_schema_cli status` が `ready` で、`retired_objects`・`pending_versions`・`pending_destructive_migrations` が空。
   - 保存値に `full` が残っていない（0 件）。

     ```sql
     SELECT COUNT(*) FROM rag_documents WHERE JSON_VALUE(processing_config, '$.graph_profile') = 'full';
     SELECT COUNT(*) FROM rag_document_recipes WHERE JSON_VALUE(processing_config, '$.graph_profile') = 'full';
     SELECT COUNT(*) FROM rag_knowledge_bases WHERE JSON_VALUE(retrieval_config, '$.ingestion.graph_profile') = 'full';
     ```

### 旧 `full` で構築した文書

- ナレッジベースの「文書構造グラフ」は、再取込しなくても今までどおり表示される（残す表の中身は `entities` と同じ）。
- 文書詳細の関係情報の層は、再取込するまで「構築計画に含まれていますが、まだ実体化していません」と出る。層の ID は構築の設定値から作るため、`full` で記録した層と `entities` の層が別の ID になる。気になる文書は再取込する。`off` と `entities` の文書の層・chunk_set・索引は変わらない（層の ID の算法 `KEY_VERSION` は変えていない）。

## 既存環境の更新手順（#286: Docker Compose からネイティブ配備への移行）

#286 で、RAG の backend・ingestion-worker・前処理・parser を Docker Compose から、サービスごとの uv の venv と
systemd の unit に移した。サービス管理画面は docker compose ではなく systemd の unit を操作する。既定の解析エンジンは Docling になった。

### 何が変わるか

| 項目 | 以前（Docker Compose） | #286 以降 |
|---|---|---|
| backend / ingestion-worker | compose の `backend` / `ingestion-worker` | `production-ready-rag-backend.service` / `production-ready-rag-ingestion-worker.service`（`rag/backend/.venv`） |
| 前処理 / parser | compose の service（`rag-compose`） | `production-ready-rag-<service>.service`（`rag/services/*/*/.venv`、`127.0.0.1:<port>`） |
| 実行ユーザー | コンテナの `appuser` | 専用の system user `ragsvc`（home は `/var/lib/production-ready-rag`） |
| アップロード原本 | named volume `production-ready-rag_backend-local-storage` | `/u01/data/production-ready-rag` |
| OCI の設定 | named volume `production-ready-rag_oci-config` | `/var/lib/production-ready-rag/.oci/config` |
| モデル（docling など） | image に焼き込み | `ragsvc` の `~/.cache`（配備時に取得） |
| サービス管理の操作 | `docker compose up / stop / build / rm` | `systemctl enable --now / disable --now / restart`、`journalctl -u <unit>`（sudoers で許可した unit だけ） |
| 起動 / 停止の状態 | 再配備で compose_services.txt の service を全部起動 | 最後に操作した状態（enable / disable）を再起動・再配備でも保つ。初めての unit は起動 |
| Terraform | `rag_compose_services` / `rag_enable_parser_docling` / `compose_services.txt` | `rag_services`（Docling は常に配備）/ `rag_services.txt` |
| 既定の解析エンジン | Unstructured | Docling（`RAG_PARSER_ADAPTER_BACKEND=docling`） |

### OCI Compute（`init_script.sh` で配備した環境）

新しい stack で Compute を作り直すか、既存の Compute 上で `init_script.sh` を実行し直せば移行する。
`init_script.sh` は次を自動で行う。

1. 以前の `production-ready-rag.service`（compose）を `systemctl disable --now` で止めて消し、`rag-compose down --remove-orphans`
   でコンテナを消す（volume と image は残す）。`/usr/local/bin/rag-compose` は `rag-compose.legacy-docker` に退避する。
2. named volume `production-ready-rag_backend-local-storage` の中身を `/u01/data/production-ready-rag` へ、
   `production-ready-rag_oci-config` の中身を `/var/lib/production-ready-rag/.oci` へ写す（写し先が空のときだけ。上書きしない）。
3. `ragsvc` を作り、backend と前処理 / parser の venv・unit・sudoers を作って起動する。前処理 / parser は初めての unit なので
   全部起動する（Docker で止めていたサービスは、移行後に画面から停止し直す。以後はその状態が保たれる）。

既存の Compute 上で実行し直す場合は、cloud-init と同じく tag を固定して取得し、以前の stack が書いた入力
（`/u01/aipoc/props/backend.env`）を新しい既定に合わせてから実行する。`init_script.sh` は以前の `compose_services.txt`
（`backend` / `ingestion-worker` は読み飛ばす）も読むが、`parser-docling` が無い構成（以前の `rag_enable_parser_docling=false`）は
拒否するので、そのときは `compose_services.txt` に `parser-docling` を足す。

```bash
cd /u01/aipoc/no.1-production-ready-suite
sudo -u opc git fetch --depth 1 origin <suite-v の tag> && sudo -u opc git checkout -f FETCH_HEAD
# 既定の解析エンジン（Docling）と、画面からの起動 / 停止を有効にする（stack の新しい既定と同じ）
sudo sed -i -e 's/^RAG_PARSER_ADAPTER_BACKEND=.*/RAG_PARSER_ADAPTER_BACKEND=docling/' \
  -e '/^RAG_PARSER_UNSTRUCTURED_ENABLED=/d' \
  -e 's/^RAG_SERVICE_CONTROL_ENABLED=.*/RAG_SERVICE_CONTROL_ENABLED=true/' /u01/aipoc/props/backend.env
grep -q '^RAG_PARSER_DOCLING_ENABLED=' /u01/aipoc/props/backend.env \
  || echo 'RAG_PARSER_DOCLING_ENABLED=true' | sudo tee -a /u01/aipoc/props/backend.env
sudo APP_ROOT=/u01/aipoc bash rag/init_script.sh      # ログは /var/log/rag-init.log
```

移行後の確認と片付け:

```bash
systemctl list-units --all 'production-ready-rag-*'
sudo journalctl -u production-ready-rag-backend.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/api/health
sudo ls /u01/data/production-ready-rag /var/lib/production-ready-rag/.oci
```

- サービス管理画面で各サービスの状態（稼働中 / 起動中 / 停止 / 起動失敗 / 未登録）とログ（journalctl）が見え、起動 / 停止できる。
- 文書をアップロードして取込が成功する（既定の解析エンジンは Docling）。
- 問題が無ければ Docker の資源を消してよい（任意）: `docker volume ls -q | grep '^production-ready-rag_'`、
  `docker images --format '{{.Repository}}' | grep '^production-ready-rag-'` を確認して削除し、Docker 自体が不要なら
  `sudo apt-get purge docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin` で外す。
- 画面から保存した値（`platform/.env`・`model-settings.json`・`backend/.env`・`backend/parser-settings.json`）は host のファイルなので、そのまま引き継がれる。

### ローカル（Docker Compose で前処理 / parser を動かしていた環境）

1. 以前のコンテナを止めて消す（port 18010〜18038 を空ける）。#310 より前の project 名（旧 repo の
   `no1-production-ready-rag`、monorepo の `rag/` で compose を使った場合の `rag`）のコンテナも同じく消す。
   label で絞るので、別 project のコンテナは消えない。

   ```bash
   for project in production-ready-rag no1-production-ready-rag rag; do
     docker ps -aq --filter "label=com.docker.compose.project=${project}" | xargs -r docker rm -f
   done
   ```

2. 「[ローカル開発 › 前処理 / parser](#前処理--parseruv-の-venv--systemd286)」の手順で unit と sudoers を登録する
   （`scripts/rag-services.sh install`）。`scripts/build-services.sh`、compose ファイル、Dockerfile は削除した（#356）。
3. `backend/.env` の `RAG_SERVICE_CONTROL_COMMAND` はもう読まない（削除してよい）。サービスの URL
   （`RAG_PARSER_*_SERVICE_URL` / `RAG_PREPROCESS_*_SERVICE_URL`）を docker 名（`http://parser-docling:8000` など）で
   書いている場合は、`backend/.env.example` と同じ `127.0.0.1:<port>` に直すか行を消す（#356 から読み替えない）。
4. backend をコンテナで動かしていた場合は、named volume（`production-ready-rag_backend-local-storage` など）の中身を
   `PLATFORM_LOCAL_STORAGE_DIR`（既定 `/u01/data/production-ready-rag`）へ写す。モデルのキャッシュの volume は写さなくてよい
   （ネイティブのサービスは初回に実行ユーザーの `~/.cache` へ取得する）。確認後に volume と image を消してよい
   （`docker volume ls -q | grep -E '^(production-ready-rag|no1-production-ready-rag|rag)_'`、
   `docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^(production-ready-rag|no1-production-ready-rag)-'`）。

### 既定の解析エンジンの変更（Unstructured → Docling）

- `RAG_PARSER_ADAPTER_BACKEND` と、画面で保存した解析エンジン（#1701 からは `backend/parser-settings.json`）が無い環境は、Docling が既定になる。
  Unstructured を使い続ける場合は「取込・検索・回答設定 › 文書解析」で Unstructured を選んで保存する（または `RAG_PARSER_ADAPTER_BACKEND=unstructured`）。
- Docling は PDF と画像だけを解析する。それ以外の形式は取込を始める前に止まるので、「[既定の解析エンジン（Docling）で扱える形式と、それ以外の形式の取り込み方](#既定の解析エンジンdoclingで扱える形式とそれ以外の形式の取り込み方286)」のとおり処理レシピで Unstructured（または Office→PDF）を選ぶ。
- Unstructured の解析サービスは既定では配備しない（stack の `rag_enable_parser_unstructured`）。Docker で `parser-unstructured` を動かしていた Compute は、入力を有効にしてから `init_script.sh` を実行すると unit が作られる。入力を有効にしないと、以前の `compose_services.txt` を読む場合を除き unit は作られない。
- 抽出レシピの ID は解析エンジンを含むため、既定のままの文書は次の取込から再抽出になる。

## 既存環境の更新手順（#596 標準の回答フローのテーブルの削除）

#595 で標準の回答フロー（`standard`）のコードを削除した後、#596 で、それだけが使っていたデータベースのオブジェクトを片付けた。システムテーブルの更新（migration `20260930_005_retire_standard_engine_objects`）が次を行う。あるものだけを消し、無ければ何もしない（冪等）。

- **削除するテーブル**: `rag_prompt_versions`（system prompt の版）・`rag_generation_settings`（回答スタイルの設定）・`rag_agent_memories`（Agent Memory）。index・制約はテーブルと一緒に消える（sequence は持っていない）。システムテーブルの全再作成でも、残っていれば消す。
- **削除する行**: `rag_role_permissions` のメニュー権限 `menu.settings_grounding` / `menu.settings_generation` / `menu.settings_agentic`（#595 から読み込み時に捨てていた値。共通のロール管理の権限昇格の判定が生の値を読むため、行も消す）。

### 残すもの

- **`rag_answer_records.answer_engine` の列**: 保存済みの回答の記録（履歴）が、どの回答の方式で作られたかを表す。新しい記録には常に同じ値を書く。
- **監査 `rag_search_audit` の標準の回答フローの内訳の列**（`memory_plan_id`・`agent_memory_*` など）: 既存の監査の行を変えないため残し、今は既定値を書く。
- **文書構造索引の構築（`rag_graph_*`）と navigation の要約の取り込み**: ナレッジベースの関係図と文書詳細が使い、関係情報は `RAG_GRAPH_EXPANSION_ENABLED`（既定 OFF。#2055）を ON にしたときに回答の検索も読む（関係情報のうち、読む経路の無かった claims / community summary の表は #621 で削除した）。

### 手順

**システムテーブルを更新すると、3 つのテーブルの行は戻せない。** `rag_agent_memories` は更新の前に書き出す。`init_script.sh` の再実行と画面の「システム設定 > データベース > RAG システムテーブル」の更新も、システムテーブルを更新する（`init_script.sh` は `system_schema_cli initialize` を実行する）ので、書き出してから行う。この migration はデータを削除するので、#619 から更新は承認（CLI の `--allow-destructive`・画面の確認ダイアログ）が無ければ止まる（上の「既存環境の更新手順の共通の注意」）。

1. #596 の版のコードを取得する（`git pull` など）。まだ backend の再起動・`init_script.sh` の再実行・システムテーブルの更新はしない。
2. `rag_agent_memories` を書き出す。書き出しの CLI（`app.rag.legacy_export`）は標準ライブラリと既存の Oracle 接続だけを使うので、取得したコードを今の venv で実行できる（import のエラーになる場合は、先に backend の venv を `uv sync --locked --no-dev` で更新する。Compute ではリポジトリの所有者 `opc` で実行する）。テーブルが無い環境では何も書かずに `"status": "table_missing"` を出して終わる。`rag_prompt_versions`・`rag_generation_settings` も残したい場合は、`--table` を変えて同じように書き出す。

   ```bash
   # OCI Compute（init_script.sh で配備した環境）
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc mkdir -p /var/lib/production-ready-rag/exports
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.legacy_export \
     --table rag_agent_memories --output /var/lib/production-ready-rag/exports/rag_agent_memories.jsonl

   # ローカル
   cd rag/backend
   uv run python -m app.rag.legacy_export --table rag_agent_memories --output ~/rag-exports/rag_agent_memories.jsonl
   ```

   出力は 1 行 1 レコードの JSON Lines（列名は小文字、日時は ISO 8601、CLOB は文字列、JSON の列は object、`embedding` は数値の配列）。書き終えてから出力先に置くので、途中で失敗したときは出力先にファイルを残さない。記憶の本文など利用者の入力を含むので、ファイルは所有者だけが読める権限（600）で作る。保管と削除は運用の規則に従う。標準出力の `row_count` が DB の件数（`SELECT COUNT(*) FROM rag_agent_memories`）と合うことを確かめる。
3. backend と frontend を再起動する（`init_script.sh` を再実行する場合は、手順 4 もあわせて行われる）。削除するテーブルはもう使わないので、システムテーブルを更新する前でも動く。
4. システムテーブルを更新する。データを削除する migration なので承認を付ける（#619）。画面の「システム設定 > データベース > RAG システムテーブル」からでもよい（「作成・更新」の確認ダイアログで「削除して更新」を押す）。

   ```bash
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.system_schema_cli initialize --allow-destructive
   ```

5. `system_schema_cli status` が `ready` で、`retired_objects` と `pending_versions` が空であることを確かめる。

## 既存環境の更新手順（#595 標準の回答フローの削除）

#594 で回答を回答フローだけにした後、#595 で標準の回答フロー（`standard`）だけが使っていたコード・画面・API・設定を削除した。回答の挙動は #594 から変わらない。旧名との互換は持たない。一覧は [rag-engine.md の「標準の回答フローの設定の削除」](./rag-engine.md)。

- **画面**: 根拠確認（`/settings/grounding`）・回答スタイル（`/settings/generation`）・高度な検索（`/settings/agentic`）を削除した。ブックマークなどの古い URL は検索方法（`/settings/retrieval`）へ移す。検索方法は「回答の検索と生成」「回答の記録の保存期間」「質問履歴」の 3 カード、回答プロンプトは回答生成のプロンプトだけ（system prompt の版の作成・有効化は削除）。検索・回答プロファイルの検索モード・検索オプション・根拠確認・回答スタイル・回答プロンプト（system prompt / 既定の言語）の欄と、RAG 検索の検索モードのチップ・検索の内訳の診断も削除した。
- **API**: `GET/PATCH /api/settings/retrieval`・`/grounding`・`/generation`・`/agentic`、`GET/POST /api/settings/prompts`・`POST /api/settings/prompts/{version_id}/activate` を削除した。`POST /api/search` などの `mode`・`strategy`・`rerank_top_n`・`generation_profile` と、チャットの送信の `mode` は、送っても 422 にせず読み捨てる。
- **メニュー権限**: `menu.settings_grounding` / `menu.settings_generation` / `menu.settings_agentic` を削除した。ロールに保存済みの値は読み込み時に捨てる（権限管理の画面にも出ない）。DB の行は #596 で削除した（上の「既存環境の更新手順（#596）」）。
- **環境変数**: 次の `RAG_*` は読まなくなった。`backend/.env` に残っていても起動し、値は使わない。次に編集するときに消してよい。
  - `RAG_CONTEXT_WINDOW_CHARS`・`RAG_CONTEXT_NEIGHBOR_WINDOW`・`RAG_CONTEXT_DIVERSITY_LAMBDA`・`RAG_CONTEXT_GROUP_EXPANSION_ENABLED`・`RAG_CONTEXT_ADAPTIVE_*`・`RAG_CONTEXT_DEPENDENCY_*`・`RAG_CONTEXT_COMPRESSION_*`（`RAG_CONTEXT_GROUP_MAX_CHUNKS` は回答フローの small-to-big が使うので残る）
  - `RAG_GROUNDING_CRAG_CONFIDENCE_THRESHOLD`・`RAG_CRAG_*`・`RAG_QUERY_EXPANSION_*`・`RAG_STREAM_REALTIME_ENABLED`・`RAG_AGENT_MEMORY_*`
  - `RAG_RETRIEVAL_STRATEGY`・`RAG_RETRIEVAL_GAP_STOP_ENABLED`・`RAG_RETRIEVAL_BUSINESS_FIT_WEIGHTING_ENABLED`・`RAG_RETRIEVAL_CORRECTIVE_ENABLED`・`RAG_REASONING_TREE_MAX_SECTIONS`・`RAG_POST_RETRIEVAL_PIPELINE`
  - `RAG_GENERATION_*`（回答スタイル・system prompt の上書き・既定の言語・カスタムプロンプトと版・設定の保存先・サービスの接続）・`RAG_AGENTIC_*`・`RAG_GROUNDING_SERVICE_*`・`RAG_RETRIEVAL_SERVICE_*`

  ```bash
  # 残っている行を確かめる（消すときは backend/.env のバックアップを取ってから編集する）
  grep -nE '^RAG_(CONTEXT_(WINDOW_CHARS|NEIGHBOR_WINDOW|DIVERSITY_LAMBDA|GROUP_EXPANSION_ENABLED|ADAPTIVE_|DEPENDENCY_|COMPRESSION_)|GROUNDING_CRAG_|CRAG_|QUERY_EXPANSION_|STREAM_REALTIME_ENABLED|AGENT_MEMORY_|RETRIEVAL_(STRATEGY|GAP_STOP_ENABLED|BUSINESS_FIT_WEIGHTING_ENABLED|CORRECTIVE_ENABLED|SERVICE_)|REASONING_TREE_MAX_SECTIONS|POST_RETRIEVAL_PIPELINE|GENERATION_|AGENTIC_|GROUNDING_SERVICE_)' backend/.env
  ```

- **サービス**: 検索・根拠確認・高度な検索・回答生成の段のサービス（`services/pipeline/{retrieval,grounding,agentic,generation}`）を削除し、サービス管理の一覧からも外した。いずれも backend の中で動く段（`deployable=False`）で systemd の unit を持たず、配備の対象ではなかったので、Compute での作業は要らない。
- **データベース**: #595 の更新では DDL・データの移行は要らない。`rag_prompt_versions`・`rag_generation_settings`・`rag_agent_memories` のテーブルは #596 で削除した（上の「既存環境の更新手順（#596）」。`rag_agent_memories` は更新の前に書き出す）。`rag_search_audit` の検索・context の内訳の列（`memory_plan_id`・`agent_memory_*` など）は残し、既定値を書く。
- **手順**: コードを更新して backend と frontend を再起動する（上の環境変数は消さなくてもよい）。

## 既存環境の更新手順（#594 回答の方式を 1 つにする）

#594 で回答の方式を、根拠照合・監査付きの回答（質問の振り分けと拡張 → 検索 → CRAG の根拠の判定 → 回答の生成 → 監査）の 1 つにし、回答エンジンの選択（`backend/.env` の `RAG_ANSWER_ENGINE` と、検索・回答プロファイルの「回答エンジン」）を削除した。旧名との互換は持たない。仕組みと設定は [rag-engine.md](./rag-engine.md) を参照。

### 変わること

- **回答の方式が全員変わる。** 次の呼び出しは、これまでの標準の回答（`RAG_ANSWER_ENGINE` の既定 `standard`）から新しい方式に変わる。
  - DEFAULT 検索・回答プロファイル（`query` が空で、全体の既定を継承していた）。
  - 「回答エンジン」を継承していた検索・回答プロファイルと、「標準」を明示していた検索・回答プロファイルの RAG 検索・チャット。
  - 検索・回答プロファイルを指定しない呼び出し（MCP の `rag_search` / `/api/search` のナレッジベースだけの指定、品質評価、staging smoke）。
  - ナレッジベースの検索テストとレシピの検索比較は、回答を作らずに検索だけを行う（#593 の検索だけの経路。LLM を呼ばない）。
- **LLM の呼び出しが増える。** 標準の回答は 1 回の回答で回答の生成が 1 回（とオプションのクエリ拡張など）だった。新しい方式は、質問の拡張 = 自動ルーティング、回答の生成方式 = CRAG のとき、質問の振り分け・CRAG の根拠の判定（根拠が足りなければ検索文を書き換えて最大 3 回）・回答の生成・監査の順に、1 回の回答で少なくとも 4 回呼ぶ。チャットで会話履歴があると質問の書き換えが 1 回、「画面目録で操作画面を探す」を有効にすると 1 回増える。回答の時間と OCI Enterprise AI の利用量が増えるので、`RAG_ANSWER_TIMEOUT_SECONDS`（既定 600 秒）と利用の上限を見直す。既定は質問の拡張 = 単純検索、回答の生成方式 = 標準 RAG（#2747）で、取込・検索・回答設定 › 検索方法の「回答の検索と生成」で変える（これらは全体設定だけで決め、検索・回答プロファイルでは上書きしない。#3053）。
- 回答の記録（`rag_answer_records`）は、すべての回答で保存する（保存期間は `RAG_ANSWER_RECORD_RETENTION_DAYS`）。品質評価の結果も新しい方式の回答になるので、更新の前の結果と比べるときは同じケースで評価し直す。
- 検索・回答プロファイルの「検索方法」「検索オプション」「根拠確認」「回答スタイル」「回答プロンプト」と、取込・検索・回答設定の同じ名前の画面の設定は、回答に使われない（#594 では入力を残し、「現在の回答では、この設定は使われません。」と表示していた。欄は #595 で削除した）。当時は回答の上書きを検索・回答プロファイルの「質問の拡張」「回答の生成方式」「根拠の前後から加える数」「回答の検索のオプション」で行ったが、これらは #3053 で全体設定だけになった（検索・回答プロファイルの保存値は適用しない）。安全チェックは今までどおり質問と回答の両方に適用する。

### 更新の前に確かめる

`backend/.env` の `RAG_ANSWER_ENGINE` を確かめる（行が無いか `standard` なら、継承していた検索・回答プロファイルもすべて変わる）。

```bash
grep '^RAG_ANSWER_ENGINE=' backend/.env
```

「回答エンジン」に標準（`standard`）を明示していた検索・回答プロファイルを、Oracle で確かめる（`profile_config` は検索・回答プロファイルの設定の JSON）。

```sql
-- 「標準」を明示していた検索・回答プロファイル（#594 の後は新しい方式で回答する）
SELECT search_answer_profile_id, name, status
  FROM rag_search_answer_profiles
 WHERE JSON_VALUE(profile_config, '$.query.answer_engine') = 'standard'
 ORDER BY name;

-- 回答エンジンの設定ごとの件数（「(継承)」は全体の既定を継承。DEFAULT 検索・回答プロファイルを含む）
SELECT NVL(JSON_VALUE(profile_config, '$.query.answer_engine'), '(継承)') AS answer_engine,
       status,
       COUNT(*) AS view_count
  FROM rag_search_answer_profiles
 GROUP BY NVL(JSON_VALUE(profile_config, '$.query.answer_engine'), '(継承)'), status
 ORDER BY answer_engine, status;
```

標準の回答と比べたい検索・回答プロファイルは、更新の前に代表の質問の回答を控えておく（品質評価のケースにしておくと、更新の後に同じケースで比べられる）。

### 手順

1. `backend/.env` から `RAG_ANSWER_ENGINE` の行を削除する（`sed -i '/^RAG_ANSWER_ENGINE=/d' backend/.env`。直前の説明のコメント行も消してよい）。行が残っていても読まない。
2. 検索・回答プロファイルに保存済みの `query.answer_engine` は読み込み時に捨て、次に保存したときに消える（DDL・データの移行は要らない）。
3. 回答の時間と利用量を見直す（上の「LLM の呼び出しが増える」）。
4. 文書分割の既定を見直す（下の「文書分割の既定」）。
5. backend を再起動する。

### 文書分割の既定

新しい回答は、親子で分割した chunk（`small_to_big`。画面の表示名は「親子階層（small-to-big）」）を前提にする。親の本文（見出しの節）を回答の文脈にし、表は行グループごとに列見出しを付けた子で探し、根拠の元の要素（`source_record_refs`）から図の切り出しと要素ごとの強調を行う。そのため、#594 で `RAG_CHUNKING_STRATEGY` の既定を `structure_aware` から `small_to_big` に変えた。

- `backend/.env` に `RAG_CHUNKING_STRATEGY` を書いていない環境（文書分割の画面で保存したことがない環境）では、レシピで分割方式を上書きしていない文書は、次に Chunk を作るときから親子で分割する。既存の chunk は自動では作り直さない。
- 親子の分割はどの解析の結果でも行う（#3377。Docling のレイアウトがあればその記録から、無ければ抽出の見出し・節・頁の構造から親子を作る。[rag-engine.md の「使い方」2.](./rag-engine.md)）。
- 構造認識で作った既存の chunk も、そのまま検索と回答に使える（同じまとまりの chunk をつないで親の代わりにする）。ただし、親が節の区切りにならない・表の列見出しを繰り返さない、ため回答の文脈が粗くなる。回答の品質を上げるには、文書の Chunk を親子階層で再作成する（chunk が変わるので embedding をやり直す。OCI Generative AI の利用が増える）。
- 構造認識のまま使う場合は、`backend/.env` に `RAG_CHUNKING_STRATEGY=structure_aware` を書く（取込・検索・回答設定 › 文書分割で構造認識を選んで保存しても同じ）。

## 既存環境の更新手順（#588 全文検索の分割方式の統合）

#588 で全文検索（Oracle Text）の検索語の分割を、Sudachi（形態素解析）と文字種の区切りを組み合わせた 1 つの方式にし、選択を削除した（分割の中身は [rag-architecture.md の「ハイブリッド検索」](./rag-architecture.md)）。旧名との互換は持たない。

- 「取込・検索・回答設定 › 検索方法」と検索・回答プロファイルの編集の「全文検索の分割方式」の選択は無くなった。
- `backend/.env` の `RAG_TEXT_SEARCH_TOKENIZER` は読まない。行が残っていても無視するが、混乱を避けるため削除する（`sed -i '/^RAG_TEXT_SEARCH_TOKENIZER=/d' backend/.env`。直前の説明のコメント行も消してよい）。
- 検索・回答プロファイルに保存済みの上書き（`query.text_search_tokenizer`）は読み込み時に捨て、次に保存したときに消える（移行の作業は要らない）。
- rag_engine を単独で使う環境の `TEXT_SEARCH_TOKENIZER`（`auto` / `regex` / `sudachi`）も読まない。辞書の設定（`TEXT_SEARCH_TOKENIZER_SUDACHI_DICT` / `_CONFIG` / `_LATIN_STEMMER`）は今までどおり使う。
- 索引（`rag_chunks_text_idx` などの `RAG_TEXT_WORLD_LEXER`）は変えていないので、作り直しは要らない。backend を再起動すると新しい分割になる。
- Sudachi の辞書（`sudachidict_full`。backend の依存に含まれる）が入っていない環境では、自動で文字種の区切りだけで分割し、backend の log に「Sudachi を使えないため、全文検索の語は文字種の区切りだけで作ります」を 1 回出す。

## 既存環境の更新手順（#566 既定のテキストモデルの必須化）

#566 で「システム設定 › モデル」の既定のテキストモデルを必須にした（並びはテキスト → Vision）。既定のテキストモデルが未設定の環境は、
「システム設定 › モデル」で既定のテキストモデルを選んで保存する（以前と同じ動きにするなら、既定の Vision モデルと同じモデル）。
保存し直すまでは、従来どおり既定の Vision モデルを使う。詳細は [platform/README.md の「既存環境の更新手順（#566）」](../../platform/README.md#既存環境の更新手順566-既定のテキストモデルの必須化)を参照。

## 既存環境の更新手順（#499 既定のモデルの変数名）

#499 で既定のモデルを「既定のテキストモデル」と「既定の Vision モデル」の 2 つに分け（#566 で 2 つとも必須にした）、
共通 `.env` の `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL` / `_LLM_MODEL` / `_VLM_MODEL` を
`PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL` / `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL` に改名した（旧名は読まない）。
`platform/.env` に既定のモデルを書いている環境は、backend と worker を止めてから
`platform/scripts/migrate_model_env_names.py`（確認 → `--apply`）で書き換え、起動し直す。OCI の parser は、backend が書き直す実行用の env を読むよう「サービス管理」画面から再起動する。旧名と新名の対応、`model-settings.json` の
扱い（旧 key は読み込み時に移す）は [platform/README.md の「既存環境の更新手順（#499）」](../../platform/README.md#既存環境の更新手順499-既定のモデルの変数名)を参照。

## 既存環境の更新手順（#211: 共通 .env への移行）

#211 で、3製品共通の設定は共通 `.env`（`platform/.env`、`PLATFORM_*`）、RAG 固有の設定は `backend/.env`（`RAG_*`）に分けた。
旧名は読まないため、この版へ更新する環境では次の手順で `.env` を移す。

1. backend と ingestion-worker を停止する（Compute の stack では
   `sudo systemctl stop production-ready-rag-backend.service production-ready-rag-ingestion-worker.service`、
   ローカルでは uvicorn を止める）。
2. 移行内容を確認する（書き換えない）。リポジトリ root で実行する。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_env_to_platform.py --product rag
   ```

   共通の変数（`OCI_*` / `ORACLE_*` / `OBJECT_STORAGE_*` / `UPLOAD_STORAGE_BACKEND` / `LOCAL_STORAGE_DIR` /
   `MODEL_SETTINGS_FILE` / Enterprise AI・Generative AI のモデル設定）は `PLATFORM_*` に改名して `platform/.env` へ、
   それ以外は `RAG_*` に改名して `backend/.env` に残る（`HF_TOKEN` / `HF_ENDPOINT` は `RAG_HUGGINGFACE_TOKEN` /
   `RAG_HUGGINGFACE_ENDPOINT`）。共通 `.env` に別の値が既にある変数は競合として表示されるので、どちらを残すか確認する。
3. 問題がなければ `--apply` を付けて書き換える（`<file>.bak-211` を作ってから書き換える）。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_env_to_platform.py --product rag --apply
   ```

4. 以前の Docker Compose の配備から移した環境では、画面から保存したモデル設定とモデル・parser の API key が
   `PLATFORM_LOCAL_STORAGE_DIR`（既定 `/u01/data/production-ready-rag`）の `model-settings.json` と同じディレクトリの `.env` に
   残っていることがある（#286 の移行で named volume `backend-local-storage` から写したもの）。`model-settings.json` を `platform/` へ、
   `PLATFORM_OCI_ENTERPRISE_AI_API_KEY`（旧 `OCI_ENTERPRISE_AI_API_KEY`）を `platform/.env` へ、`RAG_PARSER_*_API_KEY` を
   `backend/.env` へ移す（またはシステム設定画面で入力し直す）。
5. 再配備または再起動する。Compute の stack では更新した tag で `init_script.sh` を実行し直す（#286 の手順）。
   ローカルでは uvicorn を起動し直す。
6. `/api/ready` がすべて `ok` になること、システム設定画面（OCI 認証・アップロード保存先・モデル・データベース）に
   移行前の値が表示されることを確認する。確認後に `*.bak-211` を削除する。

## 既存環境の更新手順（#214 共通認証と権限管理）

RAG のログインは、`.env` の単一アカウント（`RAG_AUTH_USERNAME` / `RAG_AUTH_PASSWORD` / `RAG_AUTH_SESSION_SECRET`）から、
3製品共通の認証（構成管理者 `system_admin` と DB ユーザー）に変わった。`PLATFORM_AUTH_MODE=authenticated` の環境は、更新時に次を行う。

1. 共通 `.env` に構成管理者を設定する（NL2SQL と同じ値を共有する）。

   ```bash
   PLATFORM_ADMIN_LOGIN_USER_ID=system_admin
   PLATFORM_ADMIN_LOGIN_USER_PASSWORD=<12〜30 文字、大文字・小文字・数字を含み、admin と " を含まない>
   ```

   HTTPS で配信している場合は `PLATFORM_AUTH_COOKIE_SECURE=true` にする。`backend/.env` の
   `RAG_AUTH_USERNAME` / `RAG_AUTH_PASSWORD` / `RAG_AUTH_SESSION_SECRET` / `RAG_AUTH_SESSION_TIMEOUT_SECONDS` /
   `RAG_AUTH_COOKIE_NAME` / `RAG_AUTH_COOKIE_SECURE` はもう読まないので削除する。
2. 更新した版で起動し、システムテーブルを初期化する（`PLATFORM_*` がなければ作り、`RAG_ROLE_*` を追加する。冪等）。

   ```bash
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u ragsvc HOME=/var/lib/production-ready-rag .venv/bin/python -m app.rag.system_schema_cli initialize
   ```

   画面の「システム設定 > データベース > RAG システムテーブル」からでもよい。
3. `system_admin` でログインし、「ユーザーとロール」でロールとユーザーを作り、「セキュリティ設定 > 権限管理」で
   ロールごとのメニュー・検索・回答プロファイル・ナレッジベースを設定する。NL2SQL と同じ Oracle schema を使う場合、ユーザーとロールは NL2SQL と共有される。
4. 注意:
   - 会話と回答履歴の持ち主は、ログインしたユーザー（`user_uuid`）になる。旧方式で作った会話（持ち主は共通の `admin-user-id`）は、新しいユーザーからは見えない（移行はしない）。
   - 保存済みの回答（`rag_answer_records`）は #304 から持ち主（`user_id_hash`）で分ける。システムテーブルの初期化（migration `20260928_002_answer_record_owner`）で列を足し、既存の行はチャットの回答・検索の監査から利用者が 1 人に決まるものだけ持ち主を補う。補えなかった行は SYSTEM_ADMIN と `rag.feedback.manage` を持つ利用者だけが一覧・詳細・評価・削除できる（不要なら管理者が削除する。保存期間を設定していれば、その経過でも消える）。
   - 利用者フィードバック（`rag_citation_feedback`）は #408 から送った利用者（`user_id_hash`）で分ける。一覧・件数・集計・詳細（評価ケースの作成・承認済み FAQ への反映を含む）は、SYSTEM_ADMIN（構成管理者・local の利用者を含む）だけがすべての利用者の分を扱い、ほかのロールは自分が送った分だけを扱う（`rag.feedback.manage` を持っていても同じ。NL2SQL の実行履歴と同じ規則）。他人のフィードバックの ID を指定すると 403 になる。列は以前から保存しているので schema の変更はない。送信者の記録がない古い行は SYSTEM_ADMIN だけに見える。
   - Cookie 名が `production_ready_rag_session` から `rag_session` / `rag_csrf` に変わるため、利用者は一度ログインし直す（その後 #1565 で 3 製品共通の `pr_session` / `pr_csrf` になった。[terraform/README.md](../../terraform/README.md)「ログインのセッションの共有（#1565）」）。
   - 評価・負荷試験の CLI（`app.rag.evaluation_cli` など）が `PLATFORM_AUTH_MODE=authenticated` の API を呼ぶ場合は、ログインしたセッションが必要になる。

## 既存環境の更新手順（#1701 文書解析の設定を parser-settings.json へ分ける）

「取込・検索・回答設定 › 文書解析」で保存する解析エンジンと外部接続（Endpoint・モデル）を、3 製品で共有する
`platform/model-settings.json` の `parser_adapters` から、RAG 専用の `rag/backend/parser-settings.json`
（`RAG_PARSER_SETTINGS_FILE` で変えられる。相対パスは `backend/` 基準）へ移した。NL2SQL / Agent が先にモデル設定を
保存すると `parser_adapters` の無いファイルになり、RAG の設定の読み込み・システムテーブルの CLI・backend が失敗していた
（`ValueError: v2 以降の設定には parser_adapters が必要です。`）。表の変更（migration）は無い。

1. 更新後の RAG は `model-settings.json` の `parser_adapters` を読まない（移行・読み替えはしない。残っていても害は無い）。
   `parser-settings.json` が無いうちは `backend/.env` の `RAG_PARSER_*` と既定値（Docling）で動く。
2. 画面で既定以外の解析エンジン（Unstructured など）を保存していた環境は、更新後に
   「取込・検索・回答設定 › 文書解析」で選び直して保存する（`parser-settings.json` が作られる）。
3. Compute では `init_script.sh` が `parser-settings.json`（と `.lock`）の所有者をサービスのユーザーへ戻す。SELinux の
   context は repository の `usr_t` を受け継ぐので追加の設定は無い。`backend/.env` と同じく、Compute の中だけにあるファイル
   なので、作り直す前に残す場合は写しておく。
4. システムテーブルの CLI（`app.rag.system_schema_cli`。NL2SQL・Agent の CLI も同じ）は、ORA ではない失敗（設定の
   読み込みなど）で、secret を含まない原因の手がかり（`hint`。例外の型と message の 1 行目）を出す。

## 既存環境の更新手順（#1437 長い章節を抽出してから統合・モデルの入力の上限）

章節の拡張（#1436）で根拠の入力の上限に入らない長い章節は、質問に関係する原文だけを AI に抜き出させてから（原本と
照合し、一致しない引用は捨てる）回答の根拠にするようにした。根拠の入力の上限は、回答のモデルの入力の上限（tokens）の
割合で決める（#1436 の暫定の文字数からの推定を置き換えた）。表の変更（migration）は無い。

1. 「システム設定 › モデル」の登録モデルの「入力の上限（tokens）」を、モデルの仕様の値以下に直して保存する
   （既定 128000。値の無いモデルは 128000 として扱う）。`model-settings.json` は保存で version 5 になる（version 4 は
   そのまま読める。手で書き換えなくてよい）。3 製品で共通の設定。
2. backend を再起動する（RAG・NL2SQL・Agent。共通のモデル設定の読み込みを新しい版にする）。
3. 長い章節を読む回答では、1 回の回答で最大 8 回（検索・回答プロファイルの「原文を抜き出す呼び出しの上限」で
   1〜16 回に変えられる）、AI の呼び出しが増える。上限・回答の時間の上限で読めなかった範囲は「回答に未反映の関連章節」に
   章節ごとに示し、条件つきの回答にする。環境変数は持たない（同時に呼ぶ数は 4）。

## 既存環境の更新手順（#1436 当たった chunk の章節から親の章節へ広げる回答）

回答で、当たった chunk の章節から親の章節へ段階的に広げて文脈に入れるようにした（検索・回答プロファイルの
「当たった根拠の章節から親の章節へ広げる」。**既定 on**）。上の #1438 の章節と chunk の対応を使う。表の変更
（migration）は無い。

1. 先に #1438 の手順（下の節）でシステムテーブルを更新する。対応の表が無い環境では、章節を読めずに広げない（回答は
   続ける。backend のログに `section outline load failed`）。
2. backend を再起動する。プロファイルを使わない検索と、拡張を選んでいない（`query.section_expansion_enabled` が
   未指定 / `null`）プロファイルの検索は、根拠が足りないと判定したときに章節を広げる。1 段ごとに AI の呼び出し
   （CRAG の判定）が 1 回増え、回答の時間が延びる。上限は段 4・根拠の入力はモデルの入力の上限の 50%・回答の開始から
   600 秒（`RAG_ANSWER_TIMEOUT_SECONDS` が短ければその上限）で、プロファイルごとに変えられる。
3. 拡張を使わないプロファイルは「検索・回答設定」で「当たった根拠の章節から親の章節へ広げる」を off にして保存する
   （`false` で保存される）。環境変数は持たない（`backend/.env` に書いても読まない）。
4. 回答の生成方式が標準 RAG（`RAG_ANSWER_FLOW=standard_rag`）のときは効かない。

## 既存環境の更新手順（#2002 取込の AI の呼び出しの上限つきの並行と、エラーの種類ごとの扱い）

取込の図・表の説明（Vision）・ナビゲーション要約・Embedding を、上限つきの並行（既定 Vision 4・要約 4・Embedding 2）で
呼ぶようにした。429 は同じ段の呼び出し全体で待って並行の数を減らし、権限・モデルの設定の誤り・割り当ての使い切りは
再試行せずに取込を止める（設定の説明は「設定」の `RAG_VISION_CONCURRENCY` の項）。表の変更（migration）は無い。

1. backend・取込 worker を再起動する（`sudo systemctl restart production-ready-rag-backend production-ready-rag-ingestion-worker`）。
2. 並行の数を変えるときは `rag/backend/.env` の `RAG_VISION_CONCURRENCY`（1〜16）・`RAG_NAVIGATION_SUMMARY_CONCURRENCY`
   （1〜16）・`RAG_EMBEDDING_CONCURRENCY`（1〜8）を書く。OCI Enterprise AI・OCI Generative AI の流量の制限が低い
   tenancy で 429 が続くときは下げる（上げても、429 を受けると自動で減らす）。
3. 失敗した図を残さずに取込を失敗にしたいときは `RAG_AI_CALL_FAIL_JOB_ON_ITEM_ERROR=true` を書く。
4. PDF を段ごとに解析する文書は、段の解析が終わるとその段の図・表の Vision を始め、次の段の解析と並べる（流水線）。
   段ごとの Vision の応答は段の artifact（`<RAG_EXTRACTION_ARTIFACT_PREFIX>/<文書>/vision/<段>.json`）に残り、再実行で
   済んだ段の図を呼び直さない（この更新の前に途中まで読んだ図は残っていないため、更新の後の最初の再実行では読み直す）。
5. Vision の同時の数は、文書解析の画面の「解析後の処理」からも変えられる（`RAG_VISION_CONCURRENCY` を `backend/.env` に書く）。

## 既存環境の更新手順（#2001 PDF の段ごとの解析）

PDF（2 段以上。既定 11 ページ以上）を段（既定 10 ページ）ごとに解析サービスへ送るようにした。段ごとに状態・進捗・
抽出結果（段の artifact）を残し、失敗・中断・backend / worker の再起動の後の再実行は、成功した段を解析し直さない。
文書の詳細の「工程ごとの実行状況」は、抽出の行に「抽出: N / M ページ完了」（段が終わるたびに進む）・段の帯・図・表の説明
（Vision）の bar を出し、「抽出セグメント」の一覧は既定で畳む（失敗した段があるときだけ開く）。表の変更（migration）は無い。

- まとめた後の文書全体の整理（Docling の解析結果。原本全体を 1 回で解析した結果にも同じ規則で当てる）:
  - 見出しの階層: PDF のしおり（outline）の 6 割以上が本文の見出しに当たるときは、当たった見出しの深さをしおりの深さに
    する（chunk の `section_path` と章節の木の両方）。当たらないしおり（スライドの区分だけなど）は使わない。
  - ページの境目で切れた段落・リスト（前のページの本文が文の終わりで終わらず、次のページの本文が項目の記号・番号で
    始まらない）は、次のページの record に前の record の ID を付け、親子階層（small-to-big）の分割で同じ子 chunk に入れる。
    表の続き（列の数が同じで、続きに列見出しが無い）は、続きの表に前の表の列見出しの行を付ける（続きの行だけの chunk でも
    列の意味が分かる）。record はページごとに残すため、ページ番号・プレビューの位置は変わらない。段落の続きの図・表の読み
    飛ばしと行の位置の確認は #2273 の節。
- 既に取り込んだ文書の抽出結果・chunk は変わらない（再取込したときに段ごとの解析・整理になる）。

1. backend・取込 worker を再起動する（parser のサービスは変えていない）。
2. 段を同時に解析する数を上げるときは `rag/backend/.env` の `RAG_PDF_SEGMENT_PARSE_CONCURRENCY`（1〜4）を設定する。
   Docling のサービスは 1 つのプロセス（1 worker）で解析するため、上げると同じプロセスで段を並べて解析し、CPU・メモリの
   使用が増える（実の Docling での並行の効果は未確認。既定の 1 から変えるときは解析の時間とメモリを確かめる）。

## 既存環境の更新手順（#2003 取込 job を進捗の停滞で止め、全体の上限を 24 時間にする）

図・表の多い文書の取込が、job の全体の timeout（既定 1200 秒）で Vision の段の途中に止まっていた
（「ingestion job subprocess timed out after 1200s」）。取込の worker は job の進捗（#1999 の `progress_json`）を読み、
進捗を出す段の途中で `RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS`（既定 7200 秒）進まないときだけ子を止める。
全体の上限 `RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS` の既定は 86400 秒（1 文書 1,000 図を見込む。設定の上限は 604800 秒）。

1. #1999 のシステムテーブルの更新（`progress_json` の列）が済んでいることを確かめる（停滞の判定はこの列を読む）。
2. Compute の `rag/backend/.env` に `RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS`（旧既定の 1200 など）を書いていれば消すか、
   `86400` に書き換える（書いた値が優先され、旧い短い上限のまま止まる）。既定のままなら書かなくてよい。
3. 停滞の時間を変えるなら `RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS` を書く。解析サービス・Vision の LLM の
   timeout（再試行を含む）より短い値は起動時の検証で止まる（エラーに直す設定の名前が出る）。
4. backend と取込 worker を再起動する（`sudo systemctl restart production-ready-rag-backend production-ready-rag-ingestion-worker`）。実行中の job は worker の停止で
   `QUEUED` に戻り、再起動の後に新しい止め方で最初から実行する。前に 1200 秒で失敗した文書は、文書の詳細から再実行する。

## 既存環境の更新手順（#1999 通しジョブ内の抽出の段の進捗）

文書の詳細の「工程ごとの実行状況」で、通しジョブ内で実行する抽出の行に、ページの解析（Docling）のページの進捗と、
図・表の説明（Vision）の件数を出すようにした。取込の worker が段の進捗を job の行（`rag_ingestion_jobs.progress_json`）へ
2 秒ごとに書き、ページの進捗は解析サービスの `GET /parse/progress/{id}`（解析の呼び出しの間だけ読める）から 3 秒ごとに
読む。抽出セグメントは解析の後に作るため「処理中」で作る（「待機中」のままにしない）。

1. システムテーブルを更新する（migration `20261015_004_ingestion_job_progress`。`rag_ingestion_jobs` に列
   `progress_json`（CLOB）を無ければ足す。データを削除しないので承認は要らない）。**更新するまで、取込 job の一覧・取得は
   列が無くて失敗する**ので、backend の更新と同時に行う。
2. backend・取込 worker と parser-docling のサービスを再起動する。parser-docling が古いまま（進捗の API が 404）なら、
   解析の間は「ページの解析（Docling）: ページ数を確認しています」のままで、解析の応答の後に完了とページ数を出す
   （取込は続ける）。

## 既存環境の更新手順（#1890 実行履歴）

「AI 活用」に「実行履歴」（チャット・RAG 検索の実行の一覧と詳細）を足した。実行ごとに表 `rag_run_history` に 1 行を
残す（保存期間は回答の記録と同じ `RAG_ANSWER_RECORD_RETENTION_DAYS`）。

1. システムテーブルを更新する（migration `20261015_002_run_history`。表 `rag_run_history` と索引を無ければ作る。データを
   削除しないので承認は要らない）。更新するまで実行は記録されない（チャット・RAG 検索は続ける。backend のログに
   `run history record failed`）。画面の一覧は表が無くて失敗する。
2. backend を再起動する。更新より前の実行は一覧に出ない（遡って作らない）。
3. 新しいメニュー権限 `menu.history`（実行履歴）は既存のロールに付かない。SYSTEM_ADMIN 以外の利用者に見せるときは、
   「セキュリティ設定 > 権限管理」で付ける（見えるのは自分の実行だけ。全員の実行を見られるのは SYSTEM_ADMIN だけ）。

## 既存環境の更新手順（#1839 回答前の確認を会話に残す）

チャットの回答前の確認（3 製品共通のカード）で利用者が答えた内容・飛ばしたことを、USER のメッセージに残すようにした。

1. システムテーブルを更新する（migration `20261015_001_message_clarification`。`rag_messages` に列
   `clarification_json`（CLOB）を無ければ足す。データを削除しないので承認は要らない）。**更新するまで、チャットの送信は
   列が無くて失敗する**ので、backend の更新と同時に行う。
2. backend を再起動する。検索・回答プロファイルの設定「曖昧な質問の扱い」は既定が「確認してから回答する」で、今までと同じに
   ルールの確認・業務ガイドの条件の確認を出す。

## 既存環境の更新手順（#1438 章節と chunk の対応・MCP の章節の読み取り）

文書の詳細の「章節ナビゲーション」の章節を正本にし、章節と chunk の対応を表に持つようにした。章節を直しても chunk・
embedding・ベクトル索引は作り直さない。MCP の `rag_outline` は章節の階層・`section_id` で返し（出力の版 9。未リリースの
ため互換は持たない）、`rag_read_section` を足し、根拠に所属の章節（`section`）を付ける。

1. システムテーブルを更新する（migration `20261011_001_section_chunk_map`。表 `rag_chunk_set_sections`・
   `rag_section_chunks` と索引を無ければ作る。データを削除しないので承認は要らない）。更新するまで、MCP の
   `rag_outline` / `rag_read_section` は表を読めずに失敗し、根拠の `section` は null になる（検索・回答は続ける）。取込と
   章節の保存は対応の作り直しに失敗してもログ `section_map_refresh_failed` を残して続ける。
2. backend を再起動する。既存の文書の対応は、取込をやり直さなくても、MCP で章節を読むとき（`rag_outline` /
   `rag_read_section`）にその文書の分だけ作る。根拠の `section` は対応のある文書にだけ付くので、すぐに全部の文書に付けたい
   ときは、章節の抽出規則の画面（取込・検索・回答設定 > 文書解析の「解析後の処理」）で規則を保存し直す（すべての文書の対応を
   バックグラウンドで作り直す。chunk・embedding は変えない）。
3. Agent を再起動する（スキル `business-rag-research` が `rag_read_section` を使う。MCP 接続 `rag` のツールの一覧は
   接続ごとに読み直す）。

## 既存環境の更新手順（#1430 品質評価の job の上限を回答の回数に比例させる）

品質評価の job の全体の上限を、投入時の回答の回数（ケース × 往復）×
`RAG_ANSWER_TIMEOUT_SECONDS` にした（推論型のモデルでは 1 回の回答が数分かかり、固定の 3600 秒では 20 問程度の評価でも
打ち切られたため）。`RAG_EVALUATION_JOB_TIMEOUT_SECONDS` は「最小の上限」の意味になり（既定 3600 秒）、上限は 86400 秒を超えない。

1. backend を再起動する。`backend/.env` の `RAG_EVALUATION_JOB_TIMEOUT_SECONDS=3600`（`.env.example` から写した値）は
   そのままでよい（回答の回数から決めた上限のほうが長ければ、そちらを使う）。
2. 評価 CLI は、`--timeout` / `RAG_EVALUATION_TIMEOUT_SECONDS` を指定しなければ、job の上限（job の `time_limit_seconds`）に
   300 秒を足して待つ。自前の CI で待つ時間を固定している場合は、その CI の timeout と評価の問の数に合わせて見直す
   （待つ時間を超えると CLI が job を取り消す）。

## 既存環境の更新手順（#1349 Excel の表を 1 行 1 chunk にする）

Excel（前処理 `excel_to_json`）の行の記録の分割を変えた。これまでは同じシートの行を `chunk_size` までまとめて 1 つの chunk にしていた
（例: 8 行の台帳が `A4:F11` の 1 chunk）ため、1 行の実体（システム名など）で検索しても、その行が他の行に埋もれて上位に出なかった。

- 文書分割が構造認識（`structure_aware`）のときは、1 記録（表の 1 行・手順書の 1 手順）を 1 chunk にする。本文は表頭の列名つきの値、
  引用の場所はその行のセル範囲（`A5:F5`）。他の行・前書き（表頭より上の行）と結合せず、`chunk_size` を超える行も途中で切らない。
- 親子階層（`small_to_big`。既定）のときは、これまでは構造認識へ縮退していた（`chunk_strategy_fallback_reason=layout_missing`）が、
  行を子、同じシート（手順書は同じ章）の続く行をまとめた表の一部（`RAG_CHUNK_PARENT_TARGET_CHARS` /
  `RAG_CHUNK_PARENT_MAX_CHILDREN` まで）を親にして分割する。検索は行で当て、回答の文脈には親の本文（表の一部）を渡す。
- それ以外の分割方式（`recursive_character` など本文の文字列で分ける方式）は変えていない。

schema の変更・migration は無い。解析の結果（抽出）は変わらないので、再解析は要らない。

1. backend を更新して再起動すると、それ以降に取り込む Excel の文書と、作り直す chunk から新しい分割になる。
2. 既存の Excel の文書の chunk は自動では作り直さない（今の chunk のまま検索の対象に残る）。反映するには、文書の詳細のレシピで
   Chunk の工程から処理し直す（抽出は使い回す）。chunk が変わるので embedding をやり直す（OCI Generative AI の利用が増える）。

## 既存環境の更新手順（#1336 全文検索の索引の文字の正規化）

全文検索（Oracle Text）に索引する文字列（`rag_chunks.search_text`・`rag_feedback_details.search_text`）を、保存の時点で質問の語と同じ
文字の形（NFKC と波線・ダッシュの同一視）にした。これまでは、本文に互換文字（`①` `Ⅴ` `㈱` `㌔` `℃` など）があると、質問の側だけが
NFKC で `1` `V` `(株)` などになり、全文検索で当たらなかった（全角 / 半角の英数字・半角のカタカナは lexer が同一視するので当たっていた）。
schema の変更は無い。表示の本文・embedding は変えない。

1. backend を更新すると、それ以降に取り込む文書・作り直す chunk・保存するフィードバックは正規化した値で索引される。
2. 既存の行は、次の CLI で直す（Oracle の SQL には NFKC が無いため、行を読んで値が変わる行だけを更新する。索引は `SYNC (ON COMMIT)` で更新される）。
   先に `--dry-run` で件数を確かめる。同じ CLI を何度実行しても結果は変わらない。

   ```bash
   cd rag/backend
   uv run python -m app.rag.text_search_index_cli normalize --dry-run
   uv run python -m app.rag.text_search_index_cli normalize
   ```

## 既存環境の更新手順（#1402 実体の 1 段の拡張を既定 on にする）

> この節と下の #1388・#1362 は導入当時の更新履歴です。現在の構築は「実体・別名索引」、利用は検索方法の全体設定「実体・別名による検索拡張」で選びます。両方とも既定 OFF で、検索・回答プロファイルでは拡張を上書きしません。現在の設定は [rag-engine.md](./rag-engine.md#設定一覧) を参照してください。

回答の検索の実体の 1 段の拡張（検索・回答プロファイルの「実体でつながる根拠を 1 段広げる」）を既定 on にした
（#1362 の実サービスの再評価で、多段の根拠の連鎖が上がり、1 段の質問・同じ文書の中の参照・業務支援は下がらな
かった）。文書レシピの実体の抽出（`RAG_ENTITY_INDEX_ENABLED`。既定 `true`）は変えない。表の変更（migration）
は無い。

1. backend を再起動する。プロファイルを使わない検索と、拡張を選んでいない（`query.entity_expansion_enabled`
   が未指定 / `null`）検索・回答プロファイルの検索は、拡張を行う。明示して保存した値（`true` / `false`）は
   変えない。
2. #1388〜#1401 の画面で off に戻したプロファイルは `null`（既定）で保存されているため、on になる。拡張を使わない
   プロファイルは「検索・回答設定」で「実体でつながる根拠を 1 段広げる」を off にして保存する（`false` で保存
   される）。
3. 実体の表（migration `20261009_001_entity_layer`）が無い環境では、拡張は実体の表を読めずに何も足さない（回答は
   続ける。backend のログに `entity expansion failed`）。下の #1362 の手順 1 で更新する。実体を持つ文書が検索の
   範囲に無ければ何も足さない（SQL は 1 回）。LLM の呼び出しは増えない。

## 既存環境の更新手順（#1388 実体の層の設定を画面へ移す）

実体の層の設定を画面から変えられるようにした。文書レシピの処理設定に「実体の索引」（有効なときは「実体の
索引の列」）を、検索・回答プロファイルに「実体でつながる根拠を 1 段広げる」の開閉（既定は使わない）と、
1 回の検索で加える根拠の上限（1〜20、既定 6）を足した。**実体の索引（抽出）は既定 on にした**
（`RAG_ENTITY_INDEX_ENABLED` の既定 `true`）。拡張は既定 off のまま（#1402 で既定 on にした。上の節）。

1. **拡張の環境変数を廃止した。** `RAG_ENTITY_EXPANSION_ENABLED` と `RAG_ENTITY_EXPANSION_MAX_CHUNKS` は
   読まない（`backend/.env` に残っていても無視する。消してよい）。拡張は検索・回答プロファイルごとに選び、
   そのプロファイルの検索だけに効く。止めるときはプロファイルの開閉を off にする（backend の再起動は
   要らない）。全体の停止の切り替え（kill switch）は持たない。
2. 以前 `RAG_ENTITY_EXPANSION_ENABLED=true` で拡張を使っていた環境は、使う検索・回答プロファイルの
   「検索・回答設定」で「実体でつながる根拠を 1 段広げる」を有効にして保存する（上限を変えていたときは
   上限も選ぶ）。保存するまで拡張は行わない。
3. **実体の索引が既定 on になった。** #1362 の実体の表（migration `20261009_001_entity_layer`）が無い環境では、
   次に取り込む・再処理する文書が「実体の抽出の保存に失敗しました」で止まるため、先にシステムテーブルを更新
   する（下の #1362 の手順 1。データを削除しないので承認は要らない）。新しい配備は `init_script.sh` が適用する。
   既存の文書は再処理するまで実体を持たない（拡張を選ばなければ検索の結果は変わらない）。抽出を使わない
   環境は `backend/.env` に `RAG_ENTITY_INDEX_ENABLED=false` を書く（文書ごとならレシピで無効にする）。
4. 取込の時間は文書あたり少し増える（#1362 の評価で多段 15 文書の取込が 62 秒 → 71 秒。表の行の多い文書ほど
   実体と関連の保存が増える）。列の指定が無いときは列名で名前の列・属性の列を決める。見出しのラベル（「原因 A」
   など）は種類 `label` の別名で保存し、それだけでは拡張に使わない（#1393。下の節）。拡張を選ばなければ実体は
   検索に使わない。

## 既存環境の更新手順（#1393 実体の抽出: 見出しのラベルを拡張に使わない）

本文の見出し・列挙のラベル（「原因 A」「手順 1」「案 B」）を、別名の種類 `label` で保存するようにした。検索の
拡張は、別の文書（台帳など）が同じ別名をラベル以外の種類で持つときだけラベルを使う。表の変更（migration）は無い。

1. backend を再起動する。
2. 実体の抽出（`entity_index_enabled`）を選んで取り込み済みの文書は、ラベルが種類 `name` のまま保存されている
   ため、拡張に使われ続ける。直すときは、その文書を再処理して実体を作り直す。

## 既存環境の更新手順（#1362 実体の層: 実体の表と、検索の 1 段の拡張）

文書レシピの任意の処理「実体の抽出」（`entity_index_enabled`。既定は使わない）と、回答の検索の実体の 1 段の
拡張を足した。

1. システムテーブルを更新する（migration `20261009_001_entity_layer`。表 `rag_entities`・`rag_entity_aliases`・
   `rag_entity_chunks` と索引を無ければ作る。データを削除しないので承認は要らない）。更新するまで、回答の検索は
   実体の表を読めずに拡張を行わない（回答は続ける。backend のログに `entity expansion failed`）。実体の抽出を
   選んだレシピの取込は、表が無いため「実体の抽出の保存に失敗しました」で止まる。
2. backend を再起動する。拡張は既定で使わず、既存の文書は実体を持たないため、検索の結果は変わらない。
3. 使うときは、文書詳細の処理レシピで「実体の索引」を有効にしてその文書を再処理し（列は「実体の索引の列」
   で選べる。無ければ列名で決める）、検索・回答プロファイルで「実体でつながる根拠を 1 段広げる」を有効に
   する（#1388。下の節）。

## 既存環境の更新手順（#1248 文書の版: 新しい版に置き換えた文書を検索から外す）

文書詳細の「版」で、文書を置き換えた新しい版を記録できるようにした。置き換え済み（旧版）の文書の chunk は、既定では回答の検索の対象から外す。

1. システムテーブルを更新する（migration `20261007_001_document_superseded`。`rag_documents` に列 `superseded_by_document_id`・`superseded_at` を無ければ足す。データを削除しないので承認は要らない）。更新するまで、文書の一覧・詳細と検索は列が無いためエラーになる。
2. backend を再起動する。既存の文書はすべて今有効な版（列は NULL）のままで、検索の結果は変わらない。

## 既存環境の更新手順（#1175 チャットの回答の作成を接続から切り離す）

チャットの回答の作成を、SSE の接続から切り離した（接続が切れても作成を続け、再接続・再読込で回答を受け取れる）。
作成中の回答は `rag_messages` に `STREAMING` で保存し、段階・作成しているプロセス・heartbeat を持つ。

1. システムテーブルを更新する（migration `20261005_001_message_answer_runs`。`rag_messages` に列 `progress_json`・
   `lease_owner`・`heartbeat_at` を無ければ足し、状態の制約 `rag_messages_status_ck` に `CANCELLED`（利用者の停止）を足す。
   データを削除しないので承認は要らない）。更新するまで、チャットの送信は列が無いためエラーになる。
2. backend を再起動する。同じ利用者が同時に作成できる回答の数を変える場合は `backend/.env` の
   `RAG_CHAT_MAX_ACTIVE_ANSWERS_PER_USER`（既定 3。backend のプロセスごと）を設定する。
3. Nginx の設定を `init_script.sh` で作り直す（再購読の `GET /api/chat/conversations/{id}/messages/{質問の id}/stream` を、
   送信の SSE と同じ長い待ち時間・buffering なしの location に入れた）。作り直さなくても、heartbeat（10 秒ごと）が
   流れる間は既定の location でも購読できる。

- 作成はリクエストを受けた backend のプロセスの中で動く。Gunicorn の worker が複数のとき、再購読が別の worker に届くと
  404 になり、画面は保存済みの会話の取り直し（2 秒ごと）に切り替えて完了を待つ。処理の段階は回答の trace の API
  （`GET .../messages/{回答の id}/trace`。共通の span の表を読み直す。#3226）で受け取る。
- backend の停止・再起動では、そのプロセスで作成中の回答を「中断しました」の失敗として保存する。保存できずに残った
  作成中の回答も、会話を開いたときに heartbeat の途絶え（60 秒）で失敗にし、画面は「再送信」を出す。

## 既存環境の更新手順（#1359 チャットの処理の段階を共通の段階のイベントにする）

チャットの処理の段階の保存（`rag_messages.progress_json`）を、段階の一覧の snapshot から 3 製品共通の段階のイベントの
一覧に変えた。migration は無い（列はそのまま）。

1. backend と frontend を同時に更新して再起動する（回答の配信の SSE の `progress` は `chat_progress` に変わった）。
2. 更新の前に保存した回答の段階（旧形式）は読むときに捨てる。その回答は「処理の経過」を出さない（回答・引用は変わらない）。
3. Nginx の設定は作り直さなくてよい（段階の SSE は API の既定の location〔buffering なし・待ち 600 秒〕で配信し、
   heartbeat を 10 秒ごとに送る。1 本の接続は 600 秒で閉じ、画面が続きから張り直す）。

## 既存環境の更新手順（#390 品質評価の job）

品質評価（評価・比較）を job にした。画面と評価 CLI（nightly を含む）は job の API を使う。

1. システムテーブルを更新する（migration `20260928_005_evaluation_jobs`。表 `rag_evaluation_jobs` と index
   `rag_evaluation_jobs_status_idx`・`rag_evaluation_jobs_owner_created_idx` を無ければ作るだけで、既存の表は変えない）。
   更新するまで、評価の実行は表が無いためエラーになる（同期の `/api/evaluation/run`・`/compare` は引き続き使える）。
2. backend を再起動する。job 全体の上限の最小値を変える場合は `backend/.env` の `RAG_EVALUATION_JOB_TIMEOUT_SECONDS` を設定する
   （#1430 で、job の上限は回答の回数に比例し、この値を最小値にする）。
3. 評価 CLI を自前の CI から呼んでいる場合は、待つ時間（`--timeout` / `RAG_EVALUATION_TIMEOUT_SECONDS`。未指定なら job の
   上限 + 300 秒。#1430）が
   その CI の job の timeout に収まるようにする。以前の同期 API の URL を指定していても、CLI が job の API に読み替える。

## 既存環境の更新手順（#357 取込 job の lease と heartbeat）

取込 job（`rag_ingestion_jobs`）に lease の列（`lease_owner` / `heartbeat_at`）を追加し、実行中かどうかを
「開始からの経過時間」ではなく worker の heartbeat で判定するようにした。長い job が実行中に再キューされる
（worker が 1 つなら後ろの job が流れない・2 つ以上なら二重実行になる）ことと、worker の停止で job が
`RUNNING` のまま残ることを防ぐ。

1. システムテーブルを更新する（migration `20260928_004_ingestion_jobs_lease`。列と index
   `rag_ingestion_jobs_lease_idx` を追加するだけで、既存の行は変えない）。更新するまで取込 worker は
   「システムテーブルの作成・更新が必要」のログを出して待つ。
2. 取込 worker を再起動する（Compute の stack では `init_script.sh` の再実行が unit を書き直して restart する）。
   更新前から `RUNNING` の行は `heartbeat_at` が無いため、従来どおり `RAG_INGESTION_QUEUE_STALE_RUNNING_SECONDS`
   （開始から 300 秒）で回復する。
3. `backend/.env` で `RAG_INGESTION_QUEUE_HEARTBEAT_INTERVAL_SECONDS` / `RAG_INGESTION_QUEUE_LEASE_TTL_SECONDS` を
   変える場合は、TTL を間隔の 3 倍以上にする（満たさないと起動時にエラーになる）。

## 既存環境の更新手順（#341 文書のレシピ1の補完）

以前は文書のレシピ1（`rag_document_recipes` の slot 1）を `GET /api/documents/{id}/recipes` の中で遅延して作っていた。
#341 から GET では書き込まず、レシピ1は文書の登録・ジョブの投入・レシピの編集など書き込みの経路で作る。

1. システムテーブルを更新する（migration `20260928_003_default_document_recipes`）。レシピ行が 1 件も無い文書にだけ、
   文書の状態からレシピ1を作る。レシピ1を削除して他のレシピだけがある文書には作らない。
2. migration の前でも画面は動く。レシピ行の無い文書は、一覧・詳細でレシピ1を仮の行として返し、最初の書き込み
   （設定の保存・処理の開始など）で行を作る。

## 既存環境の更新手順（#556 項目抽出の標準の項目）

項目抽出の全体の既定に、どの文書にも通じる標準の 4 項目（文書の種類・文書タイトル・発行日・作成日・発行元・作成部署）を用意した。
定義は `backend/app/rag/extraction_field_adapter.py` の `STANDARD_FIELD_DEFINITIONS`。

- **標準の項目が有効になる条件**: 全体の既定の定義のファイル（`backend/extraction-fields.json`、`RAG_FIELD_SCHEMA_FILE` で
  場所を変えている環境はそのファイル）が**無い**環境だけ。一度も「項目の定義を保存」していない環境が当たる。
  項目抽出（`RAG_FIELD_EXTRACTION_ENABLED`。#2839 から既定で無効）が有効なら、更新後の次の取込から
  **解析した本文全体を対象に OCI Enterprise AI で抽出する。長い本文は入力の予算に収まる部分に分け、
  部分ごとにモデルを呼び出すため、1 文書で複数回になる**。分割は全文の入力トークンを減らすものではなく、
  部分ごとに項目の定義・指示を送り、前の部分の末尾も重ねるため、その分の入力も加わる。
  本文が長いほど入力トークンの総量と処理時間が増える。
- 定義を保存したことのある環境（0 件で保存した場合を含む）は、ファイルがあるため何も変わらない。
  ナレッジベースで項目を定義している文書も、今までどおりその定義を使う（KB に定義が無い文書だけが全体の既定を使う）。
- 既存の文書の抽出値は変わらない。標準の項目で抽出するのは、次に解析（再処理）したときから。
  以前に定義 0 件で取り込んだ文書は、どの定義で作ったかの記録が無いため「作り直しが必要」（#550）の印は出ない。
  標準の項目で抽出し直したい文書は、文書のレシピから再処理する。
- **標準の項目を使わない方法**（どれか 1 つ）:
  - 項目抽出そのものを止める: `backend/.env` に `RAG_FIELD_EXTRACTION_ENABLED=false`、または「取込・検索・回答設定 › 文書解析」の
    「解析後の処理」で「メタデータ/項目抽出」を無効にして保存する。
  - 項目抽出は有効のまま、全体の既定を空にする: 「抽出する項目の定義」ですべての項目を削除して「項目の定義を保存」する
    （0 件の定義を保存したファイルができ、標準の項目は使わない。KB で定義した項目は今までどおり抽出する）。
  - 自分の項目に置き換える: 項目を編集して保存する。
- 保存した定義を消して標準の項目に戻すときは、同じ画面の「標準の項目に戻す」（`DELETE /api/settings/extraction-fields`）を使う
  （定義のファイルを消す。以後は、その版の標準の項目を使う）。
- データの移行（migration）と `.env` の変更は不要。

## 既存環境の更新手順（#548 ナレッジベースごとの項目抽出の定義）

項目抽出の項目の定義をナレッジベースごとに持てるようにした（KB の詳細の「抽出する項目」）。

1. システムテーブルを更新する（migration `20260930_004_knowledge_base_extraction_fields`。`rag_knowledge_bases` に
   `extraction_fields JSON` 列を追加するだけで、既存の行は NULL のまま）。NULL の KB は全体の既定の定義
   （`extraction-fields.json`）を使うため、既存環境の抽出の挙動は変わらない。
2. 更新するまで取込 worker は「システムテーブルの作成・更新が必要」のログを出して待ち、KB の「抽出する項目」の
   読み書きは列が無いためエラーになる。backend を更新したら、システムテーブルを更新する。

## 既存環境の更新手順（#537 Vision と項目抽出を既定で有効にする）

「図・画像を AI で読み取る（Vision）」（`RAG_VISION_ENABLED`）と「メタデータ/項目抽出」（`RAG_FIELD_EXTRACTION_ENABLED`）の
既定を有効（`true`）にした。

- `backend/.env` で値を明示していない環境は、更新後に両方が有効になる。
  - Vision は、画像 1 枚ごとにモデル設定の既定の Vision モデルを呼ぶ。そのため、取込の時間と呼び出しが増える。
  - 項目抽出は、項目の定義（「取込・検索・回答設定 › 文書解析」の「解析後の処理」）が 0 件なら何もしない。
    #556 から、定義を一度も保存していない環境は標準の項目を使う（上の「#556 項目抽出の標準の項目」）。
- 無効のままにしたい環境は、`backend/.env` に `RAG_VISION_ENABLED=false` / `RAG_FIELD_EXTRACTION_ENABLED=false` を書くか、
  文書解析の画面の「解析後の処理」で無効にして保存する。文書のレシピの上書きは、今までどおり優先される。
- データの移行（migration）は不要。既存の文書は、再解析したときに新しい既定で処理される。

## 既存環境の更新手順（#497 図・画像の読み取り（Vision）の統合）

「図・画像を AI で読み取る（Vision）」を解析エンジンに依存しない設定にし、「図表 VLM 要約」を削除した。
Vision は Docling を含む全ての解析エンジンで、解析の後に backend の共通の段（`app/rag/vision.py`）がモデル設定の
既定の Vision モデルで読み取る。docling サービスは Vision を行わない（LLM を呼ばない）。データの移行（migration）は不要。

1. 全体の既定を `backend/.env` の `RAG_VISION_ENABLED` へ移す。旧名は読まない（#211 と同じく互換を持たない）。
   - `RAG_PARSER_DOCLING_VISION_ENABLED=true` だった環境は `RAG_VISION_ENABLED=true` にする。
   - `RAG_ASSET_SUMMARY_ENABLED` / `RAG_ASSET_SUMMARY_MAX_ASSETS` は削除する（図表 VLM 要約は Vision に統合した。
     読み取る画像の件数の上限は設けない）。
   - 旧名が残っていても読まないので害はない。
2. 「取込・検索・回答設定 › 文書解析」の Vision のスイッチは削除した。`model-settings.json` の
   `parser_adapters.docling_vision_enabled` は読まない（残っていても読み込みは失敗せず、次に保存すると消える）。
   画面のスイッチで有効にしていた環境は、手順 1 の `RAG_VISION_ENABLED=true` にするか、文書のレシピで Vision を有効にする。
3. 保存済みの文書レシピ・KB 構築設定は読み込み時に移す。`parser_docling_vision_enabled` は `vision_enabled` に、
   `asset_summary_enabled` は `vision_enabled` が未設定のときだけ `vision_enabled` にする。次に保存すると旧 key は消える。
4. `extraction_recipe_id` の key に Vision（`rag_vision_enabled`）を全ての解析エンジンで入れ、図表 VLM 要約を派生情報の
   レイヤーの key から外したため、layer の ID の版（`variant_keys.KEY_VERSION`）を `v5` に上げた。既存の配信中の chunk は
   そのまま検索対象に残る。Vision を有効にして読み取り直すには、その文書を再解析する。
5. docling サービスへ渡していた接頭辞なしの `OCI_ENTERPRISE_AI_*`（Vision 用）はサービス実行用の env ファイルに書かなくなった。
   backend を更新した後、サービス管理画面で docling サービスを再起動すると反映される（残っていても使わない）。

## 既存環境の更新手順（#270 文書解析エンジン Marker・Unlimited-OCR・GLM-OCR の削除）

文書解析エンジンの Marker（CPU）・Unlimited-OCR（GPU の外部 API）・GLM-OCR（GPU の外部 API）への対応を削除した。
更新時に次を確認する。データの移行（migration）は不要。

1. 保存済みの設定は読み込み時に既定へ寄せる（画面や取込は壊れない）。
   - `model-settings.json`（`parser_adapters`）や `RAG_PARSER_ADAPTER_BACKEND` に削除したエンジンが残っていれば、
     既定の解析エンジン（#286 以降は Docling）として扱う。「取込・検索・回答設定 › 文書解析」で保存し直すと旧値は消える。
   - 文書レシピ・KB 構築設定に残っていれば「global 既定の解析エンジンを継承」として扱う（分割などの他の上書きは保つ）。
     これらのエンジンで作った既存の索引はそのまま検索対象に残り、文書の取込設定には parser のずれ（再処理が必要）と表示される。
     必要な文書だけ、利用できる解析エンジンを選び直して再処理する。
2. 使われなくなった設定を削除する（読まないので残っていても害はない）。
   - `backend/.env`: `RAG_PARSER_MARKER_ENABLED` / `RAG_PARSER_MARKER_SERVICE_URL` / `RAG_PARSER_UNLIMITED_OCR_*` / `RAG_PARSER_GLM_OCR_*`
     （画面から保存した `RAG_PARSER_UNLIMITED_OCR_API_KEY` / `RAG_PARSER_GLM_OCR_API_KEY` を含む）。
3. 以前の Docker Compose の環境で `parser-marker` のコンテナが残っていれば、#286 の移行（上の「ローカル」の手順 1 は
   project のコンテナをすべて消す）で消える。Terraform の stack で `rag_enable_parser_marker=true` にして配備した Compute は、
   `${APP_ROOT}/props/compose_services.txt` から `parser-marker` を消してから `init_script.sh` を実行し直すか
   （許可していない service として拒否されるため）、stack を更新して Compute を作り直す。stack の入力 `rag_enable_parser_marker` は削除した。

## 既存環境の更新手順（#310 compose の project 名の固定と parser image の作り直し）

#310 は Docker Compose の環境向けの変更（compose の project 名を `production-ready-rag` に固定し、parser-docling /
parser-unstructured の cv2 を headless 版の `opencv-python-headless` にして image を作り直す）だった。#286 以降は Docker を
使わないため、この手順は不要。旧 project（`no1-production-ready-rag` / `rag`）のコンテナ・volume・image が残っている環境は、
「[既存環境の更新手順（#286）](#既存環境の更新手順286-docker-compose-からネイティブ配備への移行)」の「ローカル」の手順で消す。
cv2 の headless 版は各サービスの `uv.lock` に固定されている（venv を作れば反映される）。

## 本番構成

OCI Resource Manager の統合 Terraform stack（monorepo root の [`terraform/stack/`](../../terraform/README.md)、#217）は、選んだ製品を 1 台の Compute に入れ
（#1316）、NL2SQL / Agent と同じネイティブ配備（uv の venv + systemd + Nginx。#286）を作る。Docker は使わない。

3 製品は 1 台の Compute にセットで配備する（`platform/deploy/suite-init.sh` が `rag/init_script.sh` を呼ぶ。#1316。単独では配備しない）。下の表と次の点が違う。

- Compute の OS は Oracle Linux 9（#1611）。OS の package は dnf、firewall は firewalld、SELinux は enforcing のまま（repository は `usr_t`、
  frontend の dist は `httpd_sys_content_t`。`platform/deploy/suite-init.sh` の `configure_selinux`。[terraform/README.md](../../terraform/README.md)）。
- 実行ユーザーは NL2SQL / Agent と同じ `opc`（共通の `platform/.env`・Wallet・`~/.oci/config` を 3 製品で読み書きするため）。`ragsvc` は作らない
  （下の表と手順の `sudo -u ragsvc HOME=/var/lib/production-ready-rag` は `sudo -u opc` に読み替える）。sudoers は `opc` に同じ unit の操作を許可する。
- frontend は `FRONTEND_BASE_PATH=/rag/` で build し、Nginx の 1 つの site（suite）が `/rag/` で配信して `/rag/api/` を prefix を外して backend へ proxy する
  （SSE・MCP の長い timeout・ログインの上限・upload の上限は同じ）。RAG の `init_script.sh` は Nginx の site を書かない。
ADB（Oracle Autonomous AI Database）と Wallet も stack が用意し（NL2SQL / Agent と共有する）、RAG の system schema はアプリの CLI（`app.rag.system_schema_cli initialize`）で適用する。

| 構成要素 | 内容 |
|---|---|
| 実行ユーザー | 専用の system user `ragsvc`（home `/var/lib/production-ready-rag`）。リポジトリは `opc` の所有で、`opc` が git pull・uv sync・frontend の build を行う |
| Python | `uv python install 3.12`（`/opt/uv/python`。`ragsvc` も読める）。各サービスの venv は `uv sync --locked --no-dev --python 3.12` |
| backend | `production-ready-rag-backend.service`（gunicorn + UvicornWorker、`127.0.0.1:8000`、workers 2、`PLATFORM_ENVIRONMENT=production`、in-process の取込 worker は無効） |
| 取込 worker | `production-ready-rag-ingestion-worker.service`（`python -m app.rag.ingestion_worker`、`KillMode=mixed`、`RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS=60`、`TimeoutStopSec=90`。停止は下の「取込 worker の lease と停止」） |
| 前処理 / parser | `production-ready-rag-<service>.service`（サービスごとの venv の gunicorn、`127.0.0.1:<port>`）。対象は stack の `rag_services`（前処理 4 つ・parser-docling と、選べば parser-unstructured）。GPU の parser は配備しない |
| frontend | host の Node.js 24 で `platform` と `rag/frontend` を build し、Nginx が `rag/frontend/dist` を配信して `/api/` を backend へ proxy する |
| upload の上限 | Nginx の `client_max_body_size` は backend の `RAG_MAX_UPLOAD_BYTES`（`backend/.env`。既定 200 MiB）+ 10 MiB（MiB で切り上げ。Refs #306） |
| 設定 | `platform/.env`（`PLATFORM_*`）と `rag/backend/.env`（`RAG_*`）。どちらも `ragsvc` だけが読める 0600。OCI parser の unit は両方を EnvironmentFile で読む |
| サービス実行用 env | `rag/backend/service-runtime.env`（0600）。サービス管理が起動 / 再起動の前に HuggingFace 設定と実効 OCI Enterprise AI 設定を書き、前処理 / parser の unit が EnvironmentFile で読む |
| ログ | `/var/log/rag-init.log`、`journalctl -u production-ready-rag-<service>.service` |

### サービス管理画面と sudoers

画面の起動 / 停止 / 再起動 / ログは、backend（`ragsvc`）が次のコマンドだけを `sudo -n` で実行する。argv は固定で、
unit 名は backend のカタログの allowlist（前処理 / parser のうち配備したもの）で検証する（shell を通さない）。
backend・ingestion-worker の unit と、それ以外のコマンドは許可しない。状態は `systemctl show`（root 不要）と各サービスの `/health` で判定する。

```text
# /etc/sudoers.d/production-ready-rag-services（init_script.sh が生成。visudo -cf で検査してから 0440 で置く）
Cmnd_Alias RAG_SERVICE_CONTROL = /usr/bin/systemctl enable --now production-ready-rag-parser-docling.service, \
    /usr/bin/systemctl disable --now production-ready-rag-parser-docling.service, \
    /usr/bin/systemctl restart production-ready-rag-parser-docling.service, \
    /usr/bin/journalctl -u production-ready-rag-parser-docling.service -n 1000 --no-pager -o short-iso, \
    ...（配備した前処理 / parser の unit ごとに同じ 4 行）
ragsvc ALL=(root) NOPASSWD: RAG_SERVICE_CONTROL
```

- 起動 = `systemctl enable --now`、停止 = `systemctl disable --now`。利用者が最後に操作した状態を systemd の enable / disable で持ち、
  サーバーの再起動では enable の unit だけが起動する。再配備（`init_script.sh`）では、enable の unit は新しいコードで restart、
  disable の unit は停止のまま、初めての unit は起動する。stack で外したサービスの unit は止めて消す。
- 再起動（`systemctl restart`）は enable / disable を変えない。画面では稼働中・起動中・一部異常のサービスにだけ出す。
- ログは `journalctl` を 1000 行固定で取り、画面が求める行数に切り詰める（sudoers と argv を一致させるため）。
- 失敗の区別: unit が登録されていない → 404、sudoers の許可が無い・systemd が使えない → 503、`systemctl` の失敗 → 502。
  ログが 0 行（`-- No entries --`）は 200 で空の本文。
- 画面から起動 / 停止するには `RAG_SERVICE_CONTROL_ENABLED=true`（stack は true）。false のときは状態の表示だけ。

```bash
systemctl list-units --all 'production-ready-rag-*'
sudo journalctl -u production-ready-rag-parser-docling.service -f
sudo systemctl status production-ready-rag-backend.service
```

規模が大きくなった場合の推奨:

- Frontend / Backend: 同じネイティブ配備（uv の venv + systemd + Nginx）の Compute を増やし、OCI Load Balancer で振り分ける
  （自前のコードの Docker イメージは作らない。#286 / #356）。取込 worker は row lock で二重実行しないので、別の Compute に並べてよい。
- Storage: OCI Object Storage。
- DB: Oracle AI Database（対応バージョンは [terraform/README.md](../../terraform/README.md) の「Autonomous AI Database（全製品で共有）」）。RAG チャンクは `VECTOR(1536, FLOAT32)`。
- LLM/VLM: OCI Enterprise AI。
- Embedding/Rerank: OCI Generative AI。
- Observability: Prometheus、OpenTelemetry、Langfuse gateway。`RAG_TRACE_EXPORT_HTTP_ENDPOINT` を設定すると、脱機密化済み RAG span event を非同期 HTTP JSON で転送する。
- Secret: 共通 `.env`（`platform/.env`、`PLATFORM_*`）と `backend/.env`（`RAG_*`）から読み込む。
- Audit: `app.audit` の `rag_search_audit` / `rag_ingestion_audit` 構造化ログをログ基盤へ転送し、必要に応じて Oracle audit table に永続化する。利用者（production はログインしたユーザー、local は `X-User-ID`）と、local の `X-Tenant-ID` は raw 値を保存せず hash 化し（production は `X-Tenant-ID` を使わない。#225）、`RAG_AUDIT_CONTEXT_HASH_SALT` は `.env` から注入する。

backend は production entrypoint として Gunicorn + `uvicorn.workers.UvicornWorker` を使う（unit の ExecStart）。local 開発だけ `uvicorn app.main:app --reload` を使い、本番では `/api/ready` と golden set gate で昇格判定する。
frontend build は外部 font service に依存せず、CSS の日本語第一 font stack で表示する。これにより CI / build が Google Fonts などへの外向き通信に依存しない。

## リリース前チェック

Pull Request と `main` への push では `.github/workflows/ci.yml` が以下の品質門を実行する。ローカルで先に確認する場合も同じコマンドを使う。

backend:

```bash
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
uv run bandit -c pyproject.toml -r app
uv run pip-audit
```

frontend:

```bash
cd frontend
npm test
npm run build
```

配備スクリプト（`init_script.sh` と systemd の unit・sudoers）:

```bash
bash rag/scripts/tests/init-script-deployment.test.sh
```

frontend の lint / dependency audit / Playwright E2E は `../nl2sql/` と同じく CI では実行しない。UI/UX を変更した PR では以下をローカルで実行し、結果を PR の `検証結果` に記載する(型検査は `npm run build` の `tsc --noEmit` が CI 上でも走る)。

```bash
cd frontend
npm run lint
npm audit --audit-level=moderate
npm run test:e2e
```

RAG 品質:

```bash
cp evaluation/golden-set.example.json evaluation/golden-set.json
# document id と期待キーワードを対象環境に合わせて編集してから実行する
# 評価は job で動く（#390）。投入して返った job_id で、終わるまで状態を取得する。
curl -X POST http://localhost:8000/api/evaluation/jobs/run \
  -H 'Content-Type: application/json' \
  -d @evaluation/golden-set.json
curl http://localhost:8000/api/evaluation/jobs/<job_id>
```

CI / nightly / staging 昇格では curl ではなく評価 gate CLI を使い、レスポンス JSON を artifact として保存する。

```bash
cd backend
uv run python -m app.rag.evaluation_cli \
  ../evaluation/golden-set.json \
  --api-base-url https://<staging-host> \
  --output ../evaluation/evaluation-result.json \
  --trend-output ../evaluation/evaluation-trend.json

uv run python -m app.rag.search_load_cli \
  ../evaluation/search-load.example.json \
  --api-base-url https://<staging-host> \
  --output ../evaluation/search-load-result.json \
  --trend-output ../evaluation/search-load-trend.json

uv run python -m app.rag.file_processing_golden_cli \
  ../docs/evaluation/file-processing-golden-set.json \
  --output ../evaluation/file-processing-report.json

uv run python -m app.rag.parser_adapter_contract_cli \
  --output ../evaluation/parser-adapter-compatibility.json

# --strict と file-processing staging の strict は、選んだ adapter(Docling)の parser サービスが要る。
# 動いていなければ、別の端末で `scripts/rag-services.sh run parser-docling`(rag/ で)を起動しておく。
uv run python -m app.rag.parser_adapter_contract_cli \
  --strict \
  --manifest ../docs/evaluation/file-processing-golden-set.json \
  --source-kind pdf \
  --source-kind html \
  --source-kind email \
  --source-kind office \
  --source-kind image \
  --output ../evaluation/parser-adapter-compatibility-strict.json

uv run python -m app.rag.file_processing_staging_cli \
  ../docs/evaluation/file-processing-golden-set.json \
  --output ../evaluation/file-processing-staging-report.json \
  --cleanup \
  --parser-adapter-contract-strict
```

`evaluation/golden-set.example.json`、`evaluation/search-load.example.json` は API / CLI request schema に合うテンプレートとしてテストで検証する。運用する `evaluation/golden-set.json` には `thresholds` を含める。複雑文書の case では `expected_content_kind` と `expected_section_paths` を指定し、`content_kind_hit_rate` / `section_coverage` threshold で document-level recall だけでは拾えない表・図・コード・メール・章節 lineage の退化を止める。評価の `rag_overrides` では質問の拡張・回答の生成方式・根拠の前後から加える数・rerank の有無・RRF 定数・同じ group から足す child の上限・Oracle vector target accuracy を一時的に上書きし（query expansion・context window などの旧 standard のキーは #591 / #595 で削除した。複数の設定を一度に比べる設定比較の API は #2085 で削除した）、評価 CLI は `--api-base-url` から評価 job の API（`/api/evaluation/jobs/run`）に投入し、job の完了を待つ（`--api-url` に以前の同期 API の URL を渡しても job の API に読み替える。`app/rag/evaluation_cli.py`）。search load CLI は `cases`、`repeat`、`concurrency`、`thresholds` を受け取り、`/api/search` の client/server p50/p95、error rate、回答フローの工程ごとの p95（`diagnostics.answer.execution_steps` の工程名で集計。#595 で `diagnostics.stream_stage_timings` から変えた）を query / answer 原文なしで artifact 化する。file-processing golden CLI は local parser / chunk / citation 契約に加えて parser fallback、低信頼文書率、失敗 segment 率などの取込品質 metric を検証し、OCI Enterprise AI / Oracle / Object Storage を伴う確認が残る場合は `passed: true` でも `promotion_ready: false` と `promotion_blockers` を artifact に出す。parser adapter compatibility CLI は Docling / Unstructured がインストール済みの環境だけ実 adapter remap smoke を実行し、未導入・未選択 adapter は status として記録する。artifact は source kind、status、parser backend、schema count、source-kind contract reason code だけを含み、抽出本文は保存しない。PDF/image では page lineage、image では bbox/asset lineage、HTML/email/Office では semantic/header/slide/sheet/table lineage も contract として見るため、単に element が 1 件返っただけでは合格にしない。本番昇格判定では `passed` だけでなく `promotion_ready` を必ず確認し、`pending_staging_checks` や `extraction_page_coverage` などの staging 必須 threshold が残る場合は `rag-file-processing-staging` を実行するか、CI で `fail_on_file_processing_pending=true` を指定して失敗扱いにする。file-processing staging CLI は `report.passed` でも promotion blocker が残る場合は exit `1` を返すため、Object Storage artifact cache などの必須 runtime check が skip された環境を CI で止められる。`file-processing-trend` CLI は保存済みの trend baseline と current trend を比較し、table QA / page hit / bbox / preview addressability / fallback rate / ingestion p95 / blocker count の退化を exit `1` で止める。どの CLI も gate 失敗時は exit `1`、入力不備は exit `2` を返す。tenant 分離を検証する評価では `--tenant-id` / `RAG_EVALUATION_TENANT_ID` または `RAG_SEARCH_LOAD_TENANT_ID` を設定する。tenant/user の raw 値は CLI 出力に表示しない。GitHub Actions の `RAG Evaluation Nightly` workflow は `RAG_EVALUATION_API_BASE_URL` repository variable が未設定なら skip し、設定済みなら evaluation result/trend と search-load result/trend を同じ artifact としてアップロードする。search load だけを外す場合は `workflow_dispatch` の `search_load_path` を空文字にする。

`RAG Evaluation Nightly` は API base URL が未設定でも `parser-adapter-compatibility.json` と file-processing artifact を先に作る。adapter の可用性は backend の venv の package ではなく parser サービスの `/health` で判定する(contract CLI と file-processing staging CLI は `RAG_PARSER_READINESS_PROBE_ENABLED` の指定に依らず常に。#343 / #366)。通常実行では parser サービスを起動しないため、選択済み adapter(既定は Docling)が `missing` でも status を記録するだけで gate を失敗にしない(実行環境が見つかった adapter の fallback / schema remap 失敗、選択した adapter の feature flag が OFF の設定矛盾は blocking のまま)が、`run_file_processing_staging=true` かつ `require_real_world_file_processing_manifest=true` の production staging では workflow が strict adapter contract を自動的に有効化する。単独 smoke を厳格化したい場合は `workflow_dispatch` で `parser_adapter_contract_strict=true` を指定して同じ経路を使える。strict では選択済み adapter の parser サービスに到達できる必要があり、未達は blocking failure になる(backend の venv に parser の extra は入れない)。そのため strict のときだけ、workflow は job の中で Docling の parser サービスを起動する(#366): `scripts/rag-services.sh sync parser-docling` で venv を作り(lock の torch は CPU 版。#1650)、`scripts/rag-services.sh run parser-docling` を背景で動かして `http://127.0.0.1:18020/health` が ok になるまで待ってから gate を実行し、最後に止める。サービスのログは artifact の `parser-docling-service.log` に残す。Docling のモデル(`~/.cache/docling`・`~/.cache/huggingface`)は docling の `uv.lock` を key に `actions/cache` で持ち、uv の cache は setup-uv が backend と docling の `uv.lock` を key に持つ(`uv cache prune --ci` 後)。strict でも Unstructured は選択されない(status は `ignored` で blocking にならない)ため起動しない。ローカルの実測(空のキャッシュ)は、sync とモデルの取得で約 60 秒、`run` の再 sync と起動で約 11 秒、strict gate で約 30〜40 秒(マシンの負荷が高いときは、それぞれ約 160 秒・約 45 秒・約 180 秒)。golden manifest の strict は、Docling の PDF 2 件と画像 1 件の schema remap を検証し、Docling が扱わない Office / HTML / email は `unsupported`(blocking にしない)として記録する。Office / HTML / email の adapter を strict で検証するのは、Unstructured を選んだときの follow-up。strict が有効な場合、workflow は parser adapter contract CLI へ `--manifest ../${file_processing_manifest_path}` と `--strict`、file-processing staging CLI へ `--parser-adapter-contract-strict` を渡す。必要に応じて `parser_adapter_contract_source_kinds=pdf,html,email,office,image` のように対象 source kind を絞る。strict mode は CLI の `--strict` と同じく adapter backend を `auto` 相当にし、Docling / Unstructured の feature flag を有効化した runtime snapshot で、manifest の `fixture_root` と `adapter_schema_remap=true` が付いた `cases[].fixture` を case 単位に実 package へ通して schema remap を検証する。file-processing staging でも同じ strict settings を preflight、実 ingestion/search client、`adapter_contract_coverage` artifact に使うため、runtime が local のままなのに adapter contract だけ合格する状態を避ける。`parser-adapter-compatibility.json` と staging payload の `parser_adapter_contract` は fixture root / fixture file name / case id を hash label に置き換えるため、非機密 real-world manifest を使っても CI artifact から顧客文書名を読めない。`--preflight-only` でも strict 時は同じ manifest fixture contract を実行し、installed/active だけで schema remap 証跡がない adapter を先に止める。production 昇格では synthetic golden manifest だけでなく、`staging_dataset_policy` 付きの非機密 real-world manifest を `file_processing_manifest_path` に指定し、`fixture_kind=real_world`、`data_sensitivity=non_sensitive`、`reviewed_for_public_ci=true`、`staging/` fixture 隔離を manifest validation で通す。workflow の `require_real_world_file_processing_manifest` は既定 true で、staging 実行時に `rag-file-processing-staging --require-real-world-policy` を渡すため、synthetic-only manifest は real OCI / Oracle client 作成前に失敗する。staging payload の `staging_dataset_policy` は manifest 合規件数に加えて `executed_real_world_case_count`、`executed_compliant_real_world_case_count`、`missing_executed_source_kinds`、`missing_executed_scenarios` も返すため、real-world case を宣言しただけで本実行から漏れた場合は promotion blocker になる。`file_processing_trend_baseline_path` / `file_processing_staging_trend_baseline_path` を指定すると、current trend と baseline trend を比較し、`file-processing-trend-regression.json` / `file-processing-staging-trend-regression.json` を artifact に保存する。staging trend 比較では `promotion_ready` だけでなく、adapter contract の scenario set / passed scenario / missing scenario / blocking scenario、backend/source passed pair、backend/scenario passed pair、backend/source/status bad count、backend/source passed count、warning code count、blocking failure reason count、executed real-world case 数、compliant executed case 数、実行済み source kind / scenario 数、missing executed source/scenario、execution error count の退化も blocker にする。package missing、adapter fallback、fixture missing、schema remap empty、trend regression などの blocking failure が残れば workflow を失敗させる。full matrix では Docling/email・Office・HTML のような非 routing 対象 pair は `unsupported` として記録するだけだが(source kind ごとの adapter の候補は、対応形式の正本 `rag_parser_core.capabilities.ADAPTER_CAPABILITIES` が宣言する backend に絞る。Docling は PDF と画像だけ。#366)、CLI で `--strict --backend docling --source-kind email` のように backend/source を明示した場合は虚偽の対応表明として blocking failure にする。

## 運用パラメータ

- `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`: 通常の構造認識・再帰文字・固定長では、既定の 800 / 120 から評価する。親子階層（small-to-big）はこの 2 つを使わず、どの解析の結果でも `RAG_CHUNK_CHILD_TARGET_CHARS` などの 5 項目で分割する([rag_poc から移植した機能のガイド](./rag-engine.md#設定一覧))。設定可能範囲は chunk size が 200-32,000 文字、overlap が 0-8,000 文字で、overlap は chunk size 未満にする。
- 見出し単位・ページ単位では、見出し/ページを第一境界として保つため 32,000 / 0 を推奨する。32,000 文字は長大な単位だけを同じ境界内で再分割する安全上限であり、chunk を常に大きくする目標値ではない。[Cohere Rerank 4](https://docs.oracle.com/en-us/iaas/Content/generative-ai/cohere-rerank-4-0.htm) の context は 32,000 token だが、文字数上限と token 上限は同一ではない。
- `RAG_CONTEXT_GROUP_MAX_CHUNKS`: 回答フローが、根拠の child と同じ `chunk_group_id` の兄弟 chunk を文脈（small-to-big）に足す上限（1〜20、既定 4）。根拠の前後から足す child の数は `RAG_NEIGHBOR_CHILD_COUNT`（[rag-engine.md の設定一覧](./rag-engine.md#設定一覧)）。旧 standard の context の設定（`RAG_CONTEXT_WINDOW_CHARS`・`RAG_CONTEXT_DIVERSITY_LAMBDA`・`RAG_CONTEXT_GROUP_EXPANSION_ENABLED`・`RAG_CONTEXT_NEIGHBOR_WINDOW`・`RAG_CONTEXT_COMPRESSION_*`）と `RAG_QUERY_EXPANSION_*`・`RAG_STREAM_REALTIME_ENABLED` は #595 で削除した（上の「既存環境の更新手順（#595）」）。
- `RAG_MIN_SIMILARITY`: recall を落としすぎないよう、評価セットで確認して調整する。
- `RAG_CANDIDATE_DIVERSITY_DEPTH` / `RAG_CANDIDATE_DIVERSITY_NEW_DOCUMENT` / `RAG_CANDIDATE_DIVERSITY_PRESENT_DOCUMENT` / `RAG_CANDIDATE_DIVERSITY_MAX_EXTRA`: 候補の多様化（#2228。既定 600 / 10 / 5 / 100）。回答の検索で検索文ごとに `DEPTH` 件を読み、融合の上位（候補の上限 150 件）は変えずに、その外から上位に無い文書の上位 `NEW_DOCUMENT` 件と、上位にある文書の続きの `PRESENT_DOCUMENT` 件を、融合の順に合計 `MAX_EXTRA` 件まで rerank の候補に足す。1 つの文書が融合の上位を占めるナレッジベース（手引きと法令など種類の違う文書）で、別の文書の根拠が候補の外に落ちるのを防ぐ。rerank の候補は 150 件から最大 250 件程度に増える（検証の環境で検索の段が 1 問あたり 3〜10 秒増えた）。`MAX_EXTRA` を外すと文書の多いナレッジベースで 600 件を超え、時間が倍になる。`DEPTH=0` で無効（今までの候補）。
- `RAG_RRF_K`: hybrid retrieval の Reciprocal Rank Fusion 定数。小さいほど上位 rank を強く優先する。golden set で keyword/vector の寄与と citation 安定性を確認して調整する。
- `RAG_EMBEDDING_CACHE_ENABLED` / `RAG_EMBEDDING_CACHE_MAX_ENTRIES` / `RAG_EMBEDDING_BATCH_SIZE`: backend process 内で OCI Generative AI embedding 結果を LRU cache する。cache key は本文そのものではなく、model id、input type、dimension、本文 SHA-256 から作る。batch 内や連続検索で同じ query/chunk が出た場合は miss だけを OCI へ送る。miss は最大 96 件かつ合計 100,000 文字の先に達した単位で OCI embedding request に分割し、返却順を元入力順へ戻す。単一入力は 100,000 文字を超えると拒否し、本文を暗黙に切り詰めない。[Cohere Embed 4](https://docs.oracle.com/en-us/iaas/Content/generative-ai/cohere-embed-4.htm) の 128k token は、実 API の応答では入力 1 件ごとの上限として検証される（超えると HTTP 400 の `inputs[i] is too long`。#2467）。文字数の予算はこれとは別の保守的な保護値である。OCI はサービスの高負荷を HTTP 400（`high load`）でも返すため、embedding はこれを一時的な不可用として再試行し、再試行ではバッチを最大 8 分割して小さい要求で送る。空の入力（OCI は HTTP 400 で拒否する）は送る前に止める。再試行しても失敗したら、応答本文・入力本文を残さずに、HTTP status・サービスのエラーコード・理由・バッチの位置・件数・文字数・UTF-8 の byte 数（token 数の上限の見積もり）を job のエラーと構造化ログ `embedding_request_failed` に残す。worker 間共有はしないため、容量と batch size は worker 数、メモリ、OCI payload limit、p95 latency を見て調整する。`MAX_ENTRIES=0` は無効化と同じ。
- `RAG_RERANK_CACHE_ENABLED` / `RAG_RERANK_CACHE_MAX_ENTRIES`: backend process 内で OCI Generative AI rerank 結果を LRU cache する。cache key は query/document 原文ではなく SHA-256、model id、top_n、document 順序から作る。候補順や top_n が変わると別 cache entry になる。頻出 FAQ / 評価実行 / 再検索の p95 latency と OCI 呼び出し数を見て調整する。`MAX_ENTRIES=0` は無効化と同じ。
- `RAG_ANSWER_TIMEOUT_SECONDS`: LLM を呼ぶ回答生成（`/api/search`・`/api/search/stream`・チャットの送信・MCP・品質評価の 1 ケース）の通しの timeout（既定 600 秒で、上限の LLM 1 回の timeout の上限と同じ。#375 / #383 / #1425。推論の量の既定 medium で推論型のモデルの 1 回の呼び出しが数十秒になるため 300 秒から広げた）。agentic（検索の計画・multi_hop の再分解）を使う検索・回答プロファイルでは LLM を最大 3 回呼ぶため、推論型のモデルの p95 latency × 呼び出し回数より長くする。画面は SSE を打ち切らず、`/api/search` の非ストリームは 630 秒、Nginx（`platform/deploy/suite-nginx.sh` が生成する回答生成・評価・MCP・業務ガイドを試すの `location`。#1597）は 660 秒待つ（backend の 504 と理由が画面に届くよう、外側ほど長くする）。
- 品質評価（golden set）は job（`/api/evaluation/jobs/run`。#390）で動く。1 ケースを `RAG_ANSWER_TIMEOUT_SECONDS` で打ち切り、時間切れになった工程をケースの結果（`error_stage` と `error_message`）に残して次のケースへ進む。job 全体は、投入時の回答の回数（ケース × 往復）× `RAG_ANSWER_TIMEOUT_SECONDS` と `RAG_EVALUATION_JOB_TIMEOUT_SECONDS`（最小値。既定 3600 秒、60〜86400）の大きいほうで打ち切り（86400 秒を超えない。#1430。推論型のモデルでは 1 回の回答が数分かかるため、固定の上限では問の数に応じて打ち切られる）、残りのケースは実行せずに失敗（`error_type=EvaluationTimeBudgetExceeded`）として結果を返す。job の上限は job の `time_limit_seconds` で返し、画面は「全体の上限 N 分」として出す。画面は job の状態を 2 秒ごとに取得して進捗（終わったケースの数 / 全体・実行中のケース・経過時間）を出し、取り消しもできる。評価 CLI は job の終わりを `RAG_EVALUATION_TIMEOUT_SECONDS`（未指定なら job の上限 + 300 秒）まで待ち、超えたら job を取り消す。
  - job は投入を受けた backend のプロセスの中で動き、状態は `rag_evaluation_jobs` に保存する（Gunicorn の別の worker に状態の取得・取り消しが届いても同じ結果になる）。backend の停止・再起動では実行中の job を失敗にし（別のプロセスは引き継がない）、プロセスが落ちて heartbeat が 120 秒途絶えた job も、次の状態の取得で失敗にする。同時に実行できる job は 2 件まで（全プロセスの合計）。終わった job は自動では消さない（#3235。以前は 7 日で消していた。結果は評価 artifact の `rag_evaluation_runs` にも残る）。
  - 以前の同期の `/api/evaluation/run` は、外部から直接呼ぶ利用のために残している。評価全体を 600 秒で打ち切り、Nginx は 660 秒待つ（#383）。
- `RAG_SEARCH_TIMEOUT_SECONDS`（品質評価の 1 ケースの上限、既定 30 秒）は #383 で削除した。品質評価の 1 ケースも `RAG_ANSWER_TIMEOUT_SECONDS` で打ち切る。既存の `rag/backend/.env` に残っていても読まれない（害はない）ので、次に編集するときに消す。
- `RAG_OCI_GUARDRAILS_TIMEOUT_SECONDS`: OCI Guardrails 検査の timeout。既定 5 秒。障害時は `regulated` が fail-closed、その他はローカル検査へ縮退し、非機密 warning と metrics / audit code を残す。
- `RAG_ORACLE_VECTOR_TARGET_ACCURACY`: Oracle AI Vector Search の問い合わせ側 `FETCH APPROX ... WITH TARGET ACCURACY`。既定は 95。staging / golden set で召回率とレイテンシを見ながら調整する。
- `RAG_ORACLE_POOL_MAX`: backend（と取込 worker）の Oracle の接続 pool の上限。既定は 16（#1490 で 4 から変更）。回答の検索は 1 問の中で、検索文をまとめた vector の SQL と keyword の SQL を並行に読み（#1498。以前は検索文ごとに 2 本）、前後の文脈・参照先などは順に読むため、1 問で使う接続は 2 本程度。同時に答える問の数 × 2〜4 を目安にする。接続は要るときに 1 本ずつ増え、ADB の session の上限（3 製品で共有）を超えないようにする。
- `RAG_ORACLE_POOL_MIN`: backend（API）の起動時に開いておく Oracle の接続の数（接続 pool の min。#1498）。既定は 4。起動直後の問で接続の作成（1 本 0.5〜1 秒）を待たないよう、起動時に pool を作り、あわせて vector index の検索の設定（target accuracy）を解決しておく（起動は待たせない）。`RAG_ORACLE_POOL_MAX` より大きければ `RAG_ORACLE_POOL_MAX` にする。取込の job のプロセスは 1 本から始める（このプロセスの数だけ ADB の session を開いたままにしない）。
- `PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT` / `PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_PATH`: OCI Enterprise AI の OpenAI-compatible gateway endpoint と LLM/VLM path。endpoint は入力どおり使い `{endpoint}{path}` を呼ぶ（path は補わない。#1440）ので、`https://inference.generativeai.<region>.oci.oraclecloud.com/openai/v1` のように `/openai/v1` まで書く（OpenAI 互換 API は `https://api.deepseek.com/v1` のように API の base URL まで）。Enterprise AI は `PLATFORM_OCI_ENTERPRISE_AI_API_KEY` による Bearer 認証で呼び出し、staging smoke で VLM/LLM 契約を確認する。Enterprise AI の model deployment / gateway response は `prediction(s)`、`output(s)`、`inference_response`、OpenAI 風 `choices`、JSON 文字列 envelope を正規化してから Pydantic schema / text 抽出へ進める。
- `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_ENDPOINT` / `_SECONDARY_PROJECT_OCID` / `_SECONDARY_API_KEY`: OCI Enterprise AI のセカンダリ接続（#533。任意。設定するときは 3 つとも必須。#542）。登録モデルごとに接続を選び、回答はテキストのモデルの接続、VLM 抽出・図の読み取りは既定の Vision モデルの接続で呼ぶ。詳細は `platform/README.md` の「OCI Enterprise AI のプライマリ接続・セカンダリ接続・ターシャリ接続」。
- `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_ENDPOINT` / `_TERTIARY_PROJECT_OCID` / `_TERTIARY_API_KEY`: ターシャリ接続（#786。任意）。OpenAI / OpenAI 互換 API 向けで、設定するときは Endpoint URL と API key が必須、Project OCID は OCI Enterprise AI に使うときだけ入れる。呼び出しは Responses API（`/responses`）。
- `PLATFORM_OCI_ENTERPRISE_AI_VLM_INPUT_MODE`: Enterprise AI VLM への入力搬送方式。`auto` は画像を inline data URL、PDF など非画像を `/files` 経由にする。`files_api` は画像も含めて VLM 入力を明示的に `/files` へアップロードし、`file_id` を `/responses` payload へ渡す。`inline_image` は画像だけ inline で送り、PDF/Office fallback など非画像は設定変更を促して停止する。`API パス` は通常 `/responses` のままにし、`/files` は endpoint から自動生成する。
- 出力の上限（Responses payload の `max_output_tokens`。#1719）: 共通の LLM / VLM の出力の上限の環境変数は持たず、「システム設定 › モデル」の登録モデルごとの「出力の上限（tokens）」（`max_output_tokens`。既定 32768。入力の上限以下）を使う。送る値は platform の `enterprise_ai_output_token_budget`（モデルの出力の上限と入力の上限の小さい方）で、テキストの呼び出し（回答・要約など）はテキストのモデル、VLM 抽出・図の読み取りは Vision のモデルの値。回答フロー（rag_engine）は回答の上限（`ANSWER_MAX_TOKENS`。既定 32768）とモデルの値の小さい方を送り、出力の上限で未完了なら 2 倍（モデルの値まで）にして 1 回、それでも未完了（広げられないときを含む）なら推論の量を 1 段下げて（high → medium → low。low は下げない）1 回だけ送り直す（#2023。未完了のたびに log の `llm_incomplete_response` に理由・上限・推論の量・使った token を WARNING で残す）。送り直しても未完了なら、画面（RAG 検索・チャット）・MCP・品質評価に「回答が長くなり出力の上限に達して途中で止まりました」の種類の文と直し方（出力の上限を大きくする・推論の量を下げる・質問を分ける）を返し、trace_id・エラー種別・工程・理由・出力の状況は「詳細」に出す。`status=incomplete` / `reason=max_output_tokens` は、理由・出力の上限・使った出力（うち推論）の token を添えた取込エラー / 回答の失敗として利用者に返す。推論のあるモデルは推論の token も出力の上限に数える。
- `RAG_PDF_SEGMENTATION_ENABLED` / `RAG_PDF_MAX_PAGES_PER_SEGMENT` / `RAG_PDF_MAX_SEGMENTS` / `RAG_PDF_SEGMENT_PARSE_CONCURRENCY`: PDF をページの段（既定 10 ページ。最大 300 段）に分け、段ごとに解析サービス（Docling・Unstructured・外部の解析 API）を呼ぶ（#2001。2 段以上の PDF だけ。OCI の解析サービス〔Document Understanding・Vision〕と PDF 以外は原本全体を 1 回で解析する）。段ごとに 待機中 → 処理中 → 完了 / 失敗 と抽出結果（段の artifact。`<RAG_EXTRACTION_ARTIFACT_PREFIX>/<文書>/segments/<段>.json`）を残し、失敗・中断・再起動の後の再実行は、成功した段の artifact（段の PDF と解析・前処理の設定の鍵が同じもの）を使って残りの段だけを解析する。段が失敗したら、始めていない段は「取消済み」にして止める。すべての段が終わったら、ページの順に 1 つの抽出結果にまとめ（ページ番号・ID・`layout_records` を原本のページにそろえ、Docling の見出しの階層は原本全体の record から作り直す）、文書全体を整える（PDF のしおりに当たる見出しの深さ・ページの境目で切れた段落・リスト・表の続き。下の #2001 の節）。段を同時に解析する数は `RAG_PDF_SEGMENT_PARSE_CONCURRENCY`（1〜4。既定 1 = 順番）。上げるときは解析サービスの CPU・メモリを確かめる。
- `RAG_PAGE_CONTINUATION_GEOMETRY_ENABLED` / `RAG_PAGE_CONTINUATION_RIGHT_EDGE_TOLERANCE` / `RAG_PAGE_CONTINUATION_INDENT_TOLERANCE` / `RAG_PAGE_CONTINUATION_WIDTH_DIFFERENCE_RATIO`: ページの境目の段落の続きの判定で、PDF の文字の層の行の位置（前の段落の最後の行が右端まで届く・次の段落の最初の行に字下げが無い・幅が近い）も確かめる（#2273。既定 true / 1.0 / 0.5 / 1.0。右端と字下げは行の高さの倍数、幅の差は狭いほうの幅の倍数）。文字の層の無いページ・縦書き・回転したページは文の終わりの規則だけで判定する。
- `RAG_PARSER_ADAPTER_BACKEND`: 任意の外部 parser adapter 選択。`local` は本プロジェクト標準 parser のみ、`auto` は有効化済み adapter を source-aware に選ぶ。PDF は `docling` → `unstructured`、画像は `unstructured` → `docling`、Office/HTML は `docling` → `unstructured`、email は `unstructured`、単純 text/markdown/csv/json は local parser を優先する。`docling` / `unstructured` を明示するとその adapter を優先するが、現行 adapter 実装が扱えない source では `*_adapter_source_unsupported` warning を残して標準 parser / Enterprise AI fallback へ戻す。adapter 出力は必ず `StructuredExtraction` / `DocumentElement` / citation metadata へ再マップし、Oracle AI Database、OCI Enterprise AI、OCI Generative AI Cohere の確定スタックは変更しない。
- `RAG_PARSER_DOCLING_ENABLED` / `RAG_PARSER_UNSTRUCTURED_ENABLED`: Docling / Unstructured adapter の feature flag。**外部 parser は services/parsers/<name> の独立サービスへ切り出した**ため、backend の venv と `scripts/start-backend.sh` は parser 依存を同期しない(`uv sync` は lean)。各 parser のバージョンは各サービスの pyproject(`docling` / `unstructured[pdf,image,docx,pptx,xlsx,csv,tsv,md]`。pi-heif・pandoc は入れない。#3351)で固定する。parser ごとに依存が大きく異なり独立して upgrade したいことが、サービス分離の理由。backend は取込時に各サービスへ HTTP 委譲し、サービス未達なら標準 parser / Enterprise AI fallback へ戻して `*_adapter_service_unreachable` を warning に出す。`RAG_PARSER_<name>_SERVICE_URL` で URL、`RAG_PARSER_SERVICE_TIMEOUT_SECONDS` で timeout、`RAG_PARSER_READINESS_PROBE_ENABLED` で readiness の /health 問い合わせを制御する。設定と導入状態は `GET /api/settings/parser-adapters` と `rag-file-processing-staging --preflight-only` の `parser_adapters` で確認する。schema remap smoke の実行証跡は必要時に `python -m app.rag.parser_adapter_contract_cli` で確認し（画面と API からは #492 で削除）、通常の readiness 取得では重い fixture parse を走らせない。両方の出力には `parser_adapter_scorecard` も含まれ、readiness と file-processing golden/staging 指標から推奨 backend を機械可読に返す。外部 adapter を staging metrics で推奨するには retrieval recall、table QA、page hit、element lineage、fallback rate の中核証拠が必要で、不足時は `adapter_metric_evidence_incomplete` を warning として返し local fallback を優先する。file-processing staging では selected adapter が未導入の場合、`parser_adapter_preflight` を失敗させ、実 OCI / Oracle client 作成前に停止する。明示 adapter が staging 指標で local fallback 未満の場合も `parser_adapter_scorecard_mismatch` を promotion blocker として返す。staging payload の `parser_adapter_source_routes` は `source_kind` ごとの `candidate_order`、`attempted_order`、`active_order`、`selected_backend`、warning を返し、PDF / image / Office / HTML / email / text の routing が CI artifact として監査できる。音声・動画はアップロードで止めるため(#3369)、routing の source kind に持たない。
  - Unstructured adapter は対応する runtime では `include_page_breaks=true`、PDF/画像では `strategy=auto` と `infer_table_structure=true` を要求する。adapter 関数の signature を見て未対応 kwargs は渡さないため、古い Unstructured API でも不要な fallback を増やさない。
  - 外部 adapter の block metadata に `parent_id` / `section_path` / heading level が含まれる場合は `DocumentElement.parent_id` / `section_path` へ再マップする。metadata が不足する場合も title block の reading order から section stack を補完し、citation / chunk lineage を保持する。
  - 外部 adapter が `FigureCaption` / `TableCaption` を parent_id なしで返す場合は、reading order 上の直前 figure / table へ parent-child lineage を補完する。figure caption は同一 chunk の `dependency_edges` へ入り、table caption も `content_kind=table` として filter / citation lineage に残す。
  - 外部 adapter の table `cells` / `table_cells` が row / col / text / bbox / span / confidence を持つ場合は `ExtractionTableCell` へ保持する。caption text より cell structure を優先して chunk text を作るため、table QA と table cell review が flat markdown だけに依存しない。
  - 外部 adapter の `Formula` / `Equation` block は `latex` / `formula` / `mathml` などの metadata から本文を復元し、`DocumentElement(content_kind=equation)` と chunk metadata の `equation_format` に残す。公式 block が `text` を持たない場合でも検索・citation から落とさない。
  - 外部 adapter の bbox は `x/y/width/height`、`x/y/w/h`、`left/top/right/bottom`、`xmin/ymin/xmax/ymax` などを `DocumentElement.bbox` / `ExtractionTableCell.bbox` / `ExtractionAsset.bbox` の `xyxy` へ正規化し、要素 chunk では `bbox_coordinate_mode` / `bbox_unit` も metadata に残す。preview overlay / citation jump / table cell review は adapter 固有の座標 key に依存しない。
  - 外部 adapter の `Image` / `Picture` / `Figure` block は `DocumentElement(content_kind=figure)` だけでなく `ExtractionAsset` にも昇格し、chunk metadata へ `asset_id` を残す。figure citation から asset export / preview audit へ辿れるようにする。
- `GET /api/documents/{document_id}/recipes/{recipe_id}/extraction-export?format=json|markdown|html|chunks`: レシピの保存済み extraction を JSON / Markdown / escaped HTML / chunk view として返す API。`chunks` は embedding を含めず、HTML は原本 HTML を実行せず escaped review source として返す。`download=true` を付けると同じ本文を `Content-Disposition: attachment`（ファイル名は `<文書名>_レシピ<N>.md|.html|.json`、chunk は `<文書名>_レシピ<N>_chunks.json`）で返す（#561）。文書の詳細の「抽出エクスポート」（Markdown / HTML / JSON のダウンロード・コピー）と「Chunk / Citation」（chunk の JSON のダウンロード）、CI artifact、parser adapter 比較の確認に使う。原本再解析や外部 parser の直接呼び出しは行わない。
  - `tables[].cells` がある表は safe `<table>` として再構成し、`data-table-id` / row / col / bbox lineage を保持する。cells がない旧 extraction は escaped `<pre>` に fallback する。
  - `assets[]` は Markdown / HTML 監査 view に `asset_id` / kind / page / bbox / alt text として表示する。HTML export では asset 実体や Object Storage path を埋め込まず、escaped text と `data-asset-id` / `data-kind` / `data-page` / `data-bbox` のみを返す。
  - `DocumentChunkView.metadata` と `RetrievedChunk.metadata` は recursive JSON metadata を保持できる。`element_ids`、`dependency_edges`、table row group、bbox などの lineage は配列/オブジェクトのまま返せるため、chunk preview / citation jump / CI artifact が文字列 split に依存しない。
- `rag-file-processing-staging` の promotion gate は、実測 metrics の合否に加えて中核閾値の弱体化も検査する。`table_qa_accuracy`、`page_hit_accuracy`、`retrieval_recall`、bbox / section / dependency / parser fallback 系の閾値が基準より緩い場合は `promotion_threshold_too_loose` で昇格を止める。
- `rag-file-processing-staging` の metrics は staging gate の実測値に加えて、local contract で証明済みの `parser_routing_accuracy`、`parser_warning_taxonomy_coverage`、`reading_order_consistency`、`table_structure_fidelity`、`visual_chunk_metadata_completeness` などを同じ artifact に統合する。これにより OCI / Oracle が必要な gate と local parser/chunker で十分検証できる gate を分離しつつ、promotion 判定は 1 つの metrics payload で完結する。
- file-processing staging の通常実行 payload には `chunk_template_scorecard` も含まれる。manifest の `expected_chunk_template` と staging/golden metrics を使い、`pdf_layout` / `office_slide` / `office_sheet` / `markdown_by_heading` / `html_semantic` / `email_thread` / `table_preserve_rows` / `ocr_page` などの template 健康度を評価する。`chunk_block_integrity`、`chunk_contextual_coherence`、`chunk_size_compliance` などの core 指標が低い場合は `chunk_template_scorecard_blocked` を promotion blocker として返す。さらに template ごとの expected / measured case count、covered / missing source kinds、covered / missing scenarios を artifact に残し、ある template の未測定を別 template の良好な aggregate 指標で隠さない。
- `RAG_INGESTION_QUEUE_STARTUP_RECOVERY_ENABLED` / `RAG_INGESTION_QUEUE_STARTUP_DRAIN_LIMIT` / `RAG_INGESTION_QUEUE_STALE_RUNNING_SECONDS` / `RAG_INGESTION_QUEUE_WORKER_CONCURRENCY` / `RAG_INGESTION_JOB_MAX_ATTEMPTS`: 永続化済み取込 job の起動時回復、stale RUNNING 判定、同時実行数、最大試行回数を制御する。同時実行数は全体の枠（`RAG_INGESTION_QUEUE_WORKER_CONCURRENCY`）と重い段の上限（`RAG_INGESTION_QUEUE_HEAVY_PHASE_CONCURRENCY`）の 2 つで、どちらも既定は `auto`（Compute の vCPU の数から決める。#2027。#2015 の固定の 4 から変えた）。vCPU は取込の worker が使える CPU の数（`os.sched_getaffinity`、無ければ `os.cpu_count`。OCI の Flex では OCPU × 2）で、全体は `max(2, vCPU − 2)`（上限 16）、重い段（ファイル準備・抽出）は `max(1, (vCPU − 2) // 3)`（全体を超えない）。例: 4 vCPU → 全体 2・重い段 1、8 vCPU → 6・2、16 vCPU → 14・4。3 製品の backend・Nginx・解析サービスに CPU を残すためで、Docling の 1 回の解析が複数のスレッドで CPU を使い、2 OCPU の Compute で解析サービスが 375% の CPU を使って load average が vCPU の 2 倍になったことによる。重い段だけを CPU の数で絞り、図の説明（Vision）・Embedding など LLM の応答待ちが多い軽い段（Chunk 作成・索引）は全体の枠の残りで並べる（段ごとの枠 / bulkhead。重い段が全体の枠を埋めて軽い段が後ろで待ち続けないようにする）。重い段の枠は、job の中の解析の区間（解析サービス・図表の読み取り・文書全体の整理）の間だけ持ち、その後の見出しの階層の推定・項目抽出（OCI Enterprise AI の応答待ち）の間は返して全体の枠だけで数える（#2702。job の中で枠を返すと worker が次の文書の解析を始める。子プロセスで動かす job は pipe で親の worker へ知らせる）。項目抽出の LLM の同時の呼び出しは、1 つの job で 1 呼び出し（段の実行器の上限）× 全体の枠までになる。数を書けばその値（1〜16）を使い、両方に数を書くときは重い段 ≤ 全体にする（超えると起動時に設定のエラー。全体だけ数を書いたときの auto の重い段は全体で頭打ち）。決めた値・元の vCPU・auto かどうかは起動時のログ（`ingestion_worker_started` の `concurrency` / `heavy_phase_concurrency` / `vcpu` / `worker_concurrency_auto` / `heavy_phase_concurrency_auto`、in-process の worker は `ingestion_inprocess_worker_enabled` にも）に出る。Compute のメモリが足りない（解析サービスが OOM で再起動する）ときは重い段を、Enterprise AI の 429 が増えるときは全体を数で下げ、`systemctl restart` で取込の worker を起動し直す。取る順番は、文書（その取込の最初の段の投入）の古い順で、続きの段（Chunk 作成・索引）を新しい文書の最初の段より先に取る（深さ優先 / パイプライン。#2027）。投入の順（FIFO）では、自動進行の次の段の job がキューの末尾に付き、一括投入の全件が 1 段ずつそろって進んでいた（最初の文書が検索できるまで全件の処理時間がかかった）。続きの段の繰り上げは最大 `RAG_INGESTION_QUEUE_PIPELINE_PRIORITY_WINDOW_SECONDS`（既定 3600 秒）で、それより前から待っている job は追い越さない（新しく投入した文書が始まらないままにならない。`0` なら投入の順）。同じ時刻なら後の段を先に取る。順番の式は `app/clients/oracle.py` の `ingestion_dispatch_order_sql` の 1 か所。worker の空きを待つ job は、文書の詳細の工程・見出しのバッジ・文書インデックスの状態列に「〈工程〉の順番待ち」と、この取る順番で前にある job の数・待っている段の枠（重い段は重い段の上限、軽い段は全体の枠）を出す（人の確認待ちとは分ける。#2026）。前の件数も同じ式（`list_ingestion_queue_positions`）で数え、重い段の job は前にある重い段の job だけ、軽い段の job は前にある軽い段の job だけを数える（重い段の上限に達した worker は重い段を除いて軽い段を取るため）。表示の同時実行数は 1 つの worker の値で、worker のプロセスを増やしたときの合計ではない。QUEUED/RUNNING job は `/api/documents/ingestion-jobs/{job_id}/cancel` で `CANCELLED` にできる。job の状態遷移（cancel・完了・失敗）は遷移元の status を条件にした UPDATE で行い、cancel 済み job を `SUCCEEDED` / `FAILED` で上書きせず、完了済み job を `CANCELLED` で上書きしない（後者は 409）。cancel API は job の状態だけを変え、文書・レシピの status は cancel を検知した worker が戻す（レシピの job はレシピ行だけを戻し、文書の status には触れない）。取り消した job の次工程は自動投入しない。`/api/documents/ingestion-jobs/{job_id}/retry` は、レシピの job なら同じレシピの job として再投入し、他のレシピの出力は初期化しない（#305）。実行中の Enterprise AI / Oracle 呼び出しを強制中断するものではないため、外部 timeout と stale recovery も併用する。`RAG_INGESTION_QUEUE_STALE_RUNNING_SECONDS` は heartbeat の無い `RUNNING` 行だけに使う（heartbeat のある行は下の lease の TTL。#357）。取込中に Oracle の接続が一時的に切れた場合（python-oracledb の `is_session_dead` / `isrecoverable`。`DPY-4011` など）は、`attempt_count < RAG_INGESTION_JOB_MAX_ATTEMPTS` なら文書・レシピの status を工程の前に戻して job を `QUEUED` に戻し、再実行する（使い切ったら `FAILED`）。DB の transaction も、commit の前に接続が切れた場合だけ新しい接続で 1 回やり直す。ログ（`ingestion_job_failed` / `ingestion_job_requeued_transient_db_error` / `oracle_transaction_retry` / `oracle_rollback_failed`）には `oracle_error_code` と `full_code` を出す（#341）。
- `RAG_INGESTION_QUEUE_HEARTBEAT_INTERVAL_SECONDS` / `RAG_INGESTION_QUEUE_LEASE_TTL_SECONDS` / `RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS`（取込 worker の lease と停止。#357）:
  - worker はプロセスごとに一意の識別子（`host:pid:乱数`）を持ち、job の claim で lease を取る（`rag_ingestion_jobs.lease_owner` / `heartbeat_at`）。
    実行中は `HEARTBEAT_INTERVAL_SECONDS`（既定 15 秒）ごとに、自分の lease の `RUNNING` job の `heartbeat_at` を更新する。heartbeat は親の worker が打つ（process isolation の子は打たない）。
  - stale の回復（worker の起動時と、アイドル時に `RAG_INGESTION_QUEUE_RECOVERY_INTERVAL_SECONDS` ごと）は、heartbeat が `LEASE_TTL_SECONDS`（既定 90 秒。間隔の 3 倍以上）を超えて途絶えた job だけを戻す。
    自分の lease の job は戻さない。`heartbeat_at` の無い行（lease 導入前の行）は従来どおり `RAG_INGESTION_QUEUE_STALE_RUNNING_SECONDS` で判定する。job の長さは、下の進捗の停滞と全体の上限（`RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS` / `RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS`）だけで切る。
    戻す UPDATE にも同じ条件を入れるため、回復の途中で heartbeat が打たれた job や完了した job は戻さない。
    heartbeat の時刻（claim の時点を含む）と TTL の判定は DB の時計（`SYSTIMESTAMP`）で行うため、worker を別の host で動かしても host の時計のずれに左右されない（#359）。
  - 完了・失敗・一時的な DB エラーでの再キューと、取り消しの後始末（文書・レシピの status の戻し）は、自分の lease の job のときだけ行う（#359）。
    heartbeat が TTL を超えて途絶え、別の worker が claim し直した後の古い実行の結果は捨て、文書・レシピも変えずに `ingestion_job_stale_result_discarded`（`job_id` / `lease_owner` / `discarded_status` / `current_status` / `current_lease_owner`）を warning で出す。
    古い実行は、工程の途中の取り消しの確認でも lease を失ったことを検知して止まる。
  - dispatch は、同じ文書の別の job が `RUNNING` の `QUEUED` job（claim できない）と、自分が実行中の job を除いて古い順に取る。先頭の claim できない job で後ろの job が止まらない。
  - 停止（SIGTERM / SIGINT）では新しい job を取らず、実行中の job の完了を `SHUTDOWN_GRACE_SECONDS`（既定 60 秒）まで待つ。終わらなければ子プロセスを止め（SIGTERM、10 秒で SIGKILL）、自分の lease の `RUNNING` job を `QUEUED` に戻す。
    claim で増やした attempt は戻し（停止は job の失敗ではない）、文書・レシピの status は stale の回復と同じく工程の前へ戻す（レシピの job はレシピ行だけ）。子の runner（`app.rag.ingestion_job_runner`）も SIGTERM で job の実行を取り消し、後始末をして終了コード 143 で終わる。
  - API プロセス内の worker（`INPROCESS_WORKER_ENABLED=true`。ローカル開発の `uvicorn --reload` など）は停止を待たせないため grace 0 で、実行中の job の子をすぐ止めて `QUEUED` に戻す。
  - systemd の worker unit は `KillMode=mixed`（SIGTERM は main process だけに送り、子は worker が止める）で、`TimeoutStopSec`（90 秒）を grace（unit で 60 秒に固定）+ 子の停止待ち 10 秒 + 戻す DB の処理より長くしている。grace を変えるときは `init_script.sh` の `WORKER_SHUTDOWN_GRACE_SECONDS` / `WORKER_TIMEOUT_STOP_SEC` を一緒に変える（テストで照合する）。
- `RAG_EVALUATION_CONCURRENCY`（品質評価で同時に実行するケースの数の上限。既定 2、1〜8。#2004）: ケースを評価ケースの順に始め、結果はケースの順のまま返す（1 ケースの往復と判定は順に行う）。回答・判定が 429（流量の制限）で失敗したら、`Retry-After` を守って評価の全体で待ち、並行の数を半分（最小 1）にして送り直す（3 回まで。送り直しは新しい trace id で記録する）。時間切れ・5xx などは回答の中の再試行のあとのケースの失敗のまま（送り直さない）。割り当ての使い切り・401・403・OCI の 404 `NotAuthorizedOrNotFound`・404（モデルが無い）・TLS の証明書の誤りは、残りのケースを始めずに評価を止め、直す所を job の失敗の理由に出す。1 は 1 件ずつ（#2004 より前と同じ）。
- `RAG_VISION_CONCURRENCY` / `RAG_NAVIGATION_SUMMARY_CONCURRENCY` / `RAG_EMBEDDING_CONCURRENCY` / `RAG_AI_CALL_BACKOFF_MAX_SECONDS` / `RAG_AI_CALL_FINAL_RETRY_DELAY_SECONDS` / `RAG_AI_CALL_FAIL_JOB_ON_ITEM_ERROR`（取込の AI の呼び出しの並行と再試行。#2002）:
  - 取込の図・表の説明（Vision）・ナビゲーション要約・Embedding（と、文書ごとに 1 回の項目抽出・検索のリランク）は、共通の部品 `pr_backend_core.bounded_calls` の上限つきの並行の実行器を通して呼ぶ。段ごとの同時の呼び出しの上限は Vision 4・ナビゲーション要約 4・Embedding 2（既定）。Vision は同じページの図・表を順番に読み（後の図の prompt は同じページの前の図の説明を含むため）、違うページを並べる。結果の順番・内容（図・項目・chunk・Embedding の入力の順）は順番に呼んだときと同じ。
  - エラーは種類に分けて扱う（HTTP status・例外の型・OCI / openai の SDK の例外・応答の本文）。429（流量の制限）は `Retry-After` を守って**同じ段の呼び出し全体で待ち**、並行の数を半分（最小 1）にして成功が続くと上限まで戻す（AIMD）。502 / 503 / 504・接続の誤り・時間切れは指数の backoff と full jitter（上限 `RAG_AI_CALL_BACKOFF_MAX_SECONDS`）で再試行し、続けば（既定 10 回）呼び出しを止めて時間をおいて試す（circuit breaker。回復しなければ止める）。500 は 2 回まで、出力の形の誤り（JSON・schema の不一致・空の応答）は 2 回まで、401 は 1 回だけ再試行する。割り当ての使い切り（429 の quota）・403・OCI の 404 `NotAuthorizedOrNotFound`・404（モデルが無い）・501 / 505・TLS の証明書の誤りは再試行せず、残りの呼び出しをせずに取込を止め、直す所（IAM policy・モデルの設定など）を文書の失敗の理由に出す。400・安全のための拒否はその件を記録して飛ばす。413・context の長さの超過は入力を小さくして（Vision は画像の解像度を半分、要約は入力を短く）やり直す。再試行の総数には予算（最初の 10 回 + 成功の数の 20%）がある。
  - 再試行を使い切った件は、段の最後に `RAG_AI_CALL_FINAL_RETRY_DELAY_SECONDS`（既定 30 秒）待ってからもう一度呼ぶ。それでも失敗した件は記録して続ける（Vision は「説明できなかった図」として要素に理由を残す）。`RAG_AI_CALL_FAIL_JOB_ON_ITEM_ERROR=true` で、その件があれば取込を失敗にする。
  - RAPTOR 要約（`RAG_RAPTOR_ENABLED=true` のとき。#2067）も同じ実行器を通す。1 つの階層の cluster を `RAG_RAPTOR_CONCURRENCY`（既定 4、1〜16）まで並べ、階層は前の階層の要約を入力にするため順に作る。429 は段の全体で待って並行の数を下げ、再試行のあとも要約できない cluster は飛ばす（leaf の chunk は残る）。権限・モデルの設定の誤り・割り当ての使い切りは、残りの cluster を呼ばずに取込を止める。
  - 再試行は実行器だけが受け持つ（SDK の再試行は切る: OCI Enterprise AI の openai SDK は `max_retries=0`、OCI Generative AI の SDK は `NoneRetryStrategy`）。`PLATFORM_OCI_ENTERPRISE_AI_MAX_RETRIES` は、実行器を通さない呼び出し（回答の生成・接続確認など）の SDK の再試行の回数のまま。
  - 待っている間も job の進捗（`progress_json` の `calls`。待ちの理由・残りの秒数・今の並行の数・再試行の数）を 10 秒ごとに更新するため、#2003 の停滞の検出は待ちを停滞と数えない。再試行ごとに種類・回数・待ちの時間をログ（`bounded_call_retry`）に出す。
  - **Vision を段ごとに解析と並べる（流水線）**: PDF を段ごとに解析するとき（#2001）、段の解析が終わるとその段の図・表の Vision を始め、次の段の解析と重ねる（解析は解析サービスの CPU、Vision は VLM の応答待ちなので重ねられる）。段の図は 1 段ずつ読み（段の中は上の並行の上限で並べる）、応答は取込の作業ディレクトリの checkpoint に残す。すべての段の後の全体の読み取り（文書全体の整理・まとめた抽出結果への書き戻し）は同じ入力の応答を使い、VLM を呼び直さない（描いたページも使い回す）。段ごとの応答は段の artifact（`<RAG_EXTRACTION_ARTIFACT_PREFIX>/<文書>/vision/<段>.json`。文書の削除で一緒に消える）にも残し、失敗・中断の後の再実行は済んだ段の図を呼び直さない（応答の鍵は入力の画像・prompt・モデルの hash なので、入力の変わった図は読み直す）。文書の詳細の「抽出の進み具合」は、解析の途中から「図・表の説明（Vision）: N / M 件（説明の済んだ段 K / S）」と、並べて説明している段・API の待ちの理由（「API の制限のため N 秒待っています」など）を出す。
  - Vision の並行の上限（`RAG_VISION_CONCURRENCY`）は、文書解析の画面の「解析後の処理」の「図・画像を AI で読み取る」の「同時に説明する図・表の数」でも変えられる（`backend/.env` に保存する）。
- `RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS` / `RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS`（取込 job の止め方。process isolation の子。#2003）:
  - 停滞（既定 7200 秒）: 親の worker は heartbeat の間隔（`RAG_INGESTION_QUEUE_HEARTBEAT_INTERVAL_SECONDS`）で job の行の進捗（`progress_json`。解析のページ・図・表の説明の件数と更新時刻。#1999）を読み、
    進捗を出す段の途中でこの秒数変わらなければ子を止めて job を失敗にする。進んでいる job は止めない。
    進捗を出さない段（抽出の前のファイル準備、抽出の後の分割・埋め込み・索引）では停滞を判定せず、それぞれの呼び出しの timeout と全体の上限に従う。
    進捗を読めない間（DB の一時的な失敗）は進んだとも止まったとも決めない（`ingestion_job_progress_read_failed` を warning で出す）。
  - 停滞の時間は、進捗を変えずに続きうる 1 回の呼び出し（再試行を含む）の最長より長くする。起動時に検証し、短ければ直す設定の名前を出して起動を止める。
    対象は解析サービス（`RAG_PARSER_SERVICE_TIMEOUT_SECONDS` × `RAG_HTTP_SERVICE_RETRY_ATTEMPTS` + 待ち。配備の unit は 1230 秒で約 6170 秒）、
    Vision の LLM（`PLATFORM_OCI_ENTERPRISE_AI_TIMEOUT_SECONDS` × (`PLATFORM_OCI_ENTERPRISE_AI_MAX_RETRIES` + 1)）。
  - 全体の上限（既定 86400 秒、設定できる上限 604800 秒〔7 日〕）: 進んでいても、これを超えたら止める保険。1 文書 1,000 図を 1 件 60 秒で順に読んでも足りる長さ。
  - 失敗の文言（job と文書の `error_message`。画面に出る）は、停滞（「取込の進捗が … 進まなかったため止めました」）と全体の上限（「取込が全体の上限（…）を超えたため止めました」）を分け、
    最後に進んだ段・件数（例「図・表の説明 120 / 232 件」）と、長くする設定の名前を出す。
  - worker が SIGKILL などで後始末できずに止まった場合は、lease の TTL 後に他の（または再起動した）worker が回復する。
- `RAG_INGESTION_QUEUE_DEDICATED_WORKER_ENABLED` / `RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED` / `RAG_INGESTION_QUEUE_POLL_INTERVAL_SECONDS`: 取込実行を API の event loop から切り離す専用ワーカー機構。`DEDICATED_WORKER_ENABLED=false`(既定)では従来どおりリクエスト後のバックグラウンドタスクで取込を実行する。`true` にすると API はキュー投入のみ行い、`app.rag.ingestion_worker.IngestionQueueWorker` がキュー(`rag_ingestion_jobs`)を `claim_ingestion_job` の row lock 付きで消費する。`INPROCESS_WORKER_ENABLED=true`(既定)なら同じ API プロセスの lifespan 内でワーカーを起動するため単一プロセスでも完結する。**ただし in-process ワーカーは Gunicorn worker プロセスごとに 1 つ起動するため、`WEB_CONCURRENCY>1` だと実効同時取込数が `WEB_CONCURRENCY × RAG_INGESTION_QUEUE_WORKER_CONCURRENCY` まで増え、OCI/Oracle を過負荷にし得る**(row lock で二重実行はしないが総並行数が乗算される)。in-process ワーカーを使う場合は `WEB_CONCURRENCY=1` にするか、API では `INPROCESS_WORKER_ENABLED=false` にして取込を別プロセスへ切り出すこと。別プロセスへ切り出す場合は `python -m app.rag.ingestion_worker` を別途起動する(本番は systemd の `production-ready-rag-ingestion-worker.service`)。起動時には `ingestion_inprocess_worker_enabled` warning ログで多重化の注意を出す。重い解析・PDF 分割・base64・チャンク・graph index・埋め込みなどの CPU/同期処理は取込・検索とも `asyncio.to_thread` でワーカースレッドへ退避し、event loop を塞がない。ワーカーは複数同時起動しても row lock により同一 job の二重実行が起きないため、worker のプロセスを増やして水平スケールできる。`POLL_INTERVAL_SECONDS` は QUEUED ジョブのポーリング間隔で、同一プロセス内の enqueue は即時起床通知で待たずに拾う。
- `PLATFORM_OCI_ENTERPRISE_AI_LLM_PAYLOAD_TEMPLATE` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_PAYLOAD_TEMPLATE`: Enterprise AI gateway ごとの request shape が標準 payload と異なる場合にだけ設定する JSON object template。文字列 placeholder は `${prompt}` / `${context}` / `${mime_type}` / `${data_base64}`、object placeholder は `"${messages}"` / `"${parameters}"` / `"${response_format}"` / `"${structured_extraction_schema}"` のように完全な文字列値として置く。未設定なら標準 payload を使い、VLM には upload metadata の MIME type を渡す。
- `PLATFORM_OCI_ENTERPRISE_AI_LLM_RESPONSE_PATH` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_RESPONSE_PATH`: Enterprise AI gateway の response が既知 envelope ではなく独自の深い JSON 構造に包まれる場合だけ指定する JSON Pointer。例: `/payload/results/0/generated/text`、`/payload/results/0/document`。未設定なら既知 envelope を自動判定する。
- backend の Gunicorn の worker 数・timeout: `init_script.sh` が作る unit（`production-ready-rag-backend.service`）の
  `--workers 2 --timeout 60 --graceful-timeout 30 --keep-alive 5`。worker 数は Compute の CPU、OCI / Oracle の p95 latency、
  同時実行数から決める。Uvicorn の worker では Gunicorn の `--timeout` は worker の生存確認で、1 件の request の長さ（`RAG_ANSWER_TIMEOUT_SECONDS` まで）を打ち切らない。
- `RAG_TRACE_EXPORT_HTTP_ENDPOINT`: 空なら構造化ログ + Prometheus のみ。設定時は `rag.trace_span` event を OpenTelemetry / Langfuse gateway へ非同期 POST する。query 本文、context 本文、OCR 原文、prompt、例外 message は送らない。
- `RAG_TRACE_EXPORT_HTTP_BEARER_TOKEN` / `RAG_TRACE_EXPORT_HTTP_TIMEOUT_SECONDS` / `RAG_TRACE_EXPORT_QUEUE_SIZE`: trace export の認証、送信 timeout、queue 上限。queue full や送信失敗は `rag_trace_export_dropped` / `rag_trace_export_failed` として記録し、RAG request は失敗させない。
- `RAG_GUARDRAIL_MAX_QUERY_CHARS`: UI 側の入力制限と合わせる。
- `RAG_RATE_LIMIT_ENABLED`: 高コスト API の app 内 limiter。外部 API Gateway / Ingress limiter と併用できる。
- `RAG_RATE_LIMIT_WINDOW_SECONDS`: fixed-window limiter の窓幅。
- `RAG_RATE_LIMIT_SEARCH_REQUESTS` / `RAG_RATE_LIMIT_EVALUATION_RUNS` / `RAG_RATE_LIMIT_UPLOADS` / `RAG_RATE_LIMIT_INGEST_REQUESTS`: tenant/user hash 単位の窓内上限。OCI / Oracle / LLM の quota と業務ピークに合わせて調整する。
- `X-RAG-Allowed-Document-Ids` / `X-RAG-Allowed-Category-Names`: `PLATFORM_AUTH_MODE=local` のときだけ使う、認証ゲートウェイまたはアプリケーション権限層が認可済み scope として backend へ付与する request header（`production` ではログインした利用者の権限から範囲を決め、この header は使わない。#214）。backend は raw 値を監査ログへ出さず、document 一覧、詳細、chunk count、retrieval に deny-by-default の scope filter として適用する。外部クライアントから直接信頼しない。
- `RAG_GUARDRAIL_MASK_SENSITIVE_IDENTIFIERS`: query / answer 内の個人番号、口座番号、電話番号、メールアドレスらしき値を `[機微情報]` にマスクする。外部 DLP と責務分担する場合だけ無効化を検討する。

安全チェックを含むリリースは、コード展開後に `uv run python -m app.rag.chat_history_sanitization --dry-run --format json` を実行し、Oracle バックアップを取得してから `--apply` する。適用後は `rag_guardrail_findings_total` の `guardrail_backend_unavailable` / `prompt_injection` / `low_groundedness` と阻止率を監視する。CLI は冪等で、原文の別バックアップをアプリ DB 内には作成しない。

履歴の浄化は会話全体の検査を終えてから更新する。OCI Guardrails が利用できないなど、検査を完了できない会話は本文・状態・警告を更新せず、その会話のメッセージ数を `failed` に数える。`failed` が 1 件以上なら CLI は終了コード 1 を返す。OCI の設定・接続が回復してから再実行する（#2879）。

## OCI へ切り替えるときの順序

1. Oracle AI Database に document / chunk / audit tables を作成する。まず backend の venv（`rag/backend`）または CI runner で `uv run python -m app.rag.oracle_schema --output ../artifacts/oracle-schema.sql --manifest-output ../artifacts/oracle-schema.manifest.json` を実行し、DDL 成果物と manifest の hash / statement 数をレビューする。既存 DB を現行 DDL 契約へ寄せる場合は `uv run python -m app.rag.oracle_schema --migration --output ../artifacts/oracle-schema-migration.sql --manifest-output ../artifacts/oracle-schema-migration.manifest.json` を実行し、migration artifact をレビューして適用する。V3 の構築 artifact(`rag_chunk_sets`、`rag_document_extractions`、`rag_artifact_layers`、`rag_kb_chunk_set_bindings`、`rag_chunks.chunk_set_id`)を既存データへ反映する場合は、あわせて `uv run python -m app.rag.variant_backfill_cli --format sql --checks-only --output ../artifacts/variant-backfill-checks.sql` と `uv run python -m app.rag.variant_backfill_cli --format json --output ../artifacts/variant-backfill.manifest.json` を生成し、[oracle-variant-backfill-runbook.md](./oracle-variant-backfill-runbook.md) の acceptance を staging artifact として保存する。生成 SQL は document table に `content_sha256`、`file_size_bytes`、`duplicate_of_document_id`、`tenant_id_hash` を含め、`content_sha256` と `tenant_id_hash, status, uploaded_at` に索引を作る。chunk table は `VECTOR(1536, FLOAT32)`、HNSW ベクトル索引(`COSINE`、目標精度 `95`、neighbors `32`、efconstruction `500`)、`tenant_id_hash`、`document_id + chunk_index`、`chunk_set_id + chunk_index` 用索引を含め、retrieval で tenant 条件、request access scope 条件、KB serving chunk_set 条件を必ず適用できるようにする。audit table は query 本文、OCR 原文、tenant/user id の raw 値を保存せず、hash、request id、trace id、guardrail code、検索件数・citation 件数、設定 fingerprint、error type を保存する（旧 standard の検索・context の内訳の列は #595 以降は既定値。既存の監査の行を変えないため列は残す。#596）。レビュー済み SQL を SQLcl や管理された migration 手順で適用してから次へ進む。

2. `ObjectStorageClient` の OCI Object Storage SDK 実装を有効化する。`PLATFORM_OBJECT_STORAGE_REGION` / `PLATFORM_OBJECT_STORAGE_NAMESPACE` / `PLATFORM_OBJECT_STORAGE_BUCKET` を設定し、保存 URI が `oci://namespace/bucket/key` になり、取得時に namespace / bucket 不一致を拒否することを staging で確認する。取込前に Object Storage から取得した bytes が document table の `file_size_bytes` / `content_sha256` と一致することも確認する。
3. `OracleClient` の python-oracledb pool、vector search、keyword search、document/chunk persistence、同じ chunk group の兄弟 chunk の取得を有効化する。`PLATFORM_ORACLE_USER` / `PLATFORM_ORACLE_DSN` / `PLATFORM_ORACLE_PASSWORD` または `PLATFORM_ORACLE_WALLET_DIR`（Thick mode では `PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin`）に配置した wallet を設定する。`INGESTING` / `ERROR` への状態遷移では該当 document の chunk/index 行と古い抽出結果を削除し、検索対象は `INDEXED` に限定する。staging では `VECTOR_DISTANCE`、`FETCH APPROX ... WITH TARGET ACCURACY`、Oracle Text `CONTAINS`、document 別 chunk count、`chunk_index` window による同一 document 前後 chunk 取得、`INDEXED` 文書が hybrid search の citation に含まれることを確認する。
4. `OciGenAiClient` の embedding / rerank は OCI Generative AI Inference SDK 実装を使う。staging では `PLATFORM_OCI_CONFIG_FILE` / profile / region / compartment / model id、Cohere Embed v4 の 1536 次元、Cohere Rerank v4 fast の返却件数・候補 index 範囲・index 重複なし・finite score を確認する。
5. `OciEnterpriseAiClient` は `PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT`、`PLATFORM_OCI_ENTERPRISE_AI_API_KEY`、既定のテキストモデル・既定の Vision モデル（画面の「モデル」、または `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL`・`PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL`。#499）、`PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH`、`PLATFORM_OCI_ENTERPRISE_AI_VLM_PATH` を使って Enterprise AI endpoint を呼び出す。標準 payload で合わない model deployment / gateway は `PLATFORM_OCI_ENTERPRISE_AI_LLM_PAYLOAD_TEMPLATE` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_PAYLOAD_TEMPLATE` で request shape を差し替え、response envelope が独自の場合は `PLATFORM_OCI_ENTERPRISE_AI_LLM_RESPONSE_PATH` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_RESPONSE_PATH` で候補 node を指定する。staging ではまず `uv run python -m app.rag.enterprise_ai_probe --surface both --dry-run` で URL、template 使用有無、response path 使用有無、payload key / shape、JSON byte 数を確認し、その後 `uv run python -m app.rag.enterprise_ai_probe --surface both` で LLM/VLM を直接呼び出す。probe は回答本文や OCR 本文を出さず、text 文字数・element 件数だけを artifact に残す。ここで Bearer 認証、timeout/retry、VLM の MIME type / 構造化抽出 JSON schema、LLM の citation-grounded 生成 payload、response parsing を実 endpoint で確認する。VLM response は `StructuredExtraction` へ検証し、LLM response は空 text を fail fast する。
6. staging にデプロイし、`/api/ready` の checks がすべて `ok` になることを確認する。production 昇格時は `PLATFORM_ENVIRONMENT=production` にして、追加 checks の `audit_context_salt` も `ok` にする。Oracle は `PLATFORM_ORACLE_USER` / `PLATFORM_ORACLE_DSN` に加えて `PLATFORM_ORACLE_PASSWORD` または `PLATFORM_ORACLE_WALLET_DIR`（Thick mode では `PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin`）に存在する Wallet が必要。
7. staging 環境でまず `uv run python -m app.rag.staging_smoke --preflight-only` を実行する。OCI/Oracle 接続設定、Enterprise AI LLM/VLM 設定、Cohere embedding/rerank 設定、`PLATFORM_UPLOAD_STORAGE_BACKEND=oci`、Object Storage namespace/bucket がそろっていることを JSON の `checks` で確認する。preflight 失敗時は外部依存へ接続せず、secret 値も出力しない。
8. preflight が `ok=true` なら `uv run python -m app.rag.staging_smoke` を実行する。Object Storage put/get、Oracle document 作成、Enterprise AI VLM、chunking、embedding、Oracle indexing、hybrid search、Enterprise AI LLM 生成を 1 回通し、作成した smoke document が citation に含まれることを確認する。既定 query は一意な `SMOKE-...` marker の原文引用を要求し、検索は新規 `document_id` に限定される。既定 query では LLM 回答にも marker が含まれない場合に `stage=rag_answer_marker` で失敗する。JSON 出力の `ok`、`marker`、`query`、`answer_contains_marker`、`trace_id`、`chunk_count`、`citation_count`、`cleanup`、`diagnostics`（`config_fingerprint`、回答フローの実行記録 `diagnostics.answer.execution_steps` など）を保存し、staging gate の artifact にする。既定では evidence として作成物を保持し、`cleanup` は `skipped` になる。DB/Object Storage を汚したくない一時確認では `uv run python -m app.rag.staging_smoke --cleanup` を使い、成功・失敗どちらでも作成済み Oracle document/chunk と Object Storage object の削除 status を確認する。失敗時は preflight の `checks` または本実行の `stage` / `cause_type` を見て、Object Storage、Oracle、取込（VLM・chunking・embedding・索引）、検索と回答（`rag_search`）、回答の確認（`rag_answer_marker`）のどこで止まったかを切り分ける。回答フローの中の工程は `diagnostics.answer.execution_steps` で確かめる。query を変える場合は `--query "確認用キーワード {marker} を要約してください"` のように `{marker}` placeholder を残す。
9. golden set 評価と負荷試験を通してから production へ昇格する。

## 共通ログ（JST）

API / worker の stderr、Nginx JSON access、共有 tail / export の契約と保持・権限確認は [共通ログ仕様](../../platform/docs/logging-standard.md) に従う（#858）。`scripts/tail-logs.sh` で製品の unit を読む。既存 Compute の Nginx は共有 template の反映と `nginx -t` が必要。実ホストの保持値は変更前に確認する。

### 回答テンプレートの placeholder の改名（#2859）

schema の更新で `20261021_001_answer_prompt_placeholders` を適用すると、保存済みの
`vlm_answer` の `{{images}}` は `{{answer_context}}`、`{{image_metadata}}` は
`{{evidence_metadata}}` になります。指示本文と更新時刻は保持し、画像読み取り専用の
`image_retrieval` は変更しません。schema の更新と backend の更新を合わせて行ってください。
ファイルに保存する SDK の構成では、`prompts/vlm_answer.txt` をバックアップしてから同じ
placeholder を置き換えます。旧名を実行時に展開する互換処理はありません。

組み込みの指示は Markdown の節・手順・条件で表示します。編集済みの独自の指示本文は
自動で書き直しません。組み込みの新しい書式へ戻す場合は、内容を確認してから設定画面の
「既定に戻す」を使います。外部根拠を囲む `BEGIN_UNTRUSTED_RETRIEVED_CONTEXT` /
`END_UNTRUSTED_RETRIEVED_CONTEXT` は引き続きモデルへ送信します。

### 安全チェックを OCI Guardrails のスイッチへ更新（#2863）

更新時に `python -m app.rag.guardrail_settings_migration --env-file .env` を実行し、
回答 backend を再読み込みします。旧 OCI 選択は on、Local 選択は off へ移ります。
設定・障害時の扱いは [安全チェック](evaluation-observability-guardrails.md) を参照してください。

## 外部 DomainProfile の読込の廃止（#3668）

`RAG_ENGINE_DOMAIN_PROFILE_FILE` と作業ディレクトリの `domain_profile.json` を読む機能、および `RAG_ANSWER_PROFILE` / `RAG_ENGINE_PROFILE` の選択を廃止した。これらの変数は別名で受け付けず、残っていても実行には使わない。backend の `.env`、起動 shell、systemd の drop-in に残る定義は運用者が削除する。`DOCRAG_DOMAIN_PROFILE_FILE` の旧定義も新しい名前へ改名しない。

ファイル自体や保存済みデータは自動削除しない。別名や判定に必要な設定は、内容を確認したうえで検索・回答プロファイルのページ管理の「用語・同義語」「ドメインキーワード」「回答ルール」「業務ガイド」へ必要な分だけ登録する。文書分類は保存済み metadata と手入力が正本となり、外部 taxonomy による名前の補正やパスからの大分類・中分類の推定は行わない。既存の chunk の分類と検索範囲は保持する。文書の再取込・DB migration は不要。

一般的な日本語の質問理解は設定に依存せず維持する。SDK から `profile=` を渡していた場合は引数を除き、Prompt 上書きだけが必要なら Runtime の `prompt_overrides` を使う。Prompt の本文や保存済みの Prompt 設定は変更しない。

## 検索する件数の既定（#3679）

`rag/backend/.env` の `RAG_SEARCH_TOP_K_DEFAULT=50` は、検索・チャット・公開 MCP・品質評価・負荷確認で `top_k` を省略したときの基準件数です（1〜100）。明示した `top_k` が優先されます。DB の取得深度、Rerank の候補総数、最終引用数は別の制約です。既定の候補多様化の深さ 600 は変更していません。

検索画面は件数を送信せず、旧 URL と作業状態の件数も使いません。実行履歴から開く場合も、質問・プロファイル・絞り込みを引き継ぎ、件数は現在の backend の設定を使います。Terraform の新規配備と再配備の生成元にも 50 を明示しています。既存環境へ追加する場合は他の設定を保って同変数を追記してください。

公開する API / MCP の省略時の値は現在の設定から生成し、静的な契約ファイルは標準の設定 50 で照合します。別製品や外部クライアントが件数を明示して呼ぶ場合、その値を変更しません。不正な環境値は Settings の検証エラーです。稼働中の `.env` 再読込に失敗したときは、既存の設定更新の規則どおり警告を記録し、最後に成功した設定を保ちます。


### 質問履歴と候補の退役（#3762）

質問を候補として集計する設定・保存処理・候補 API は廃止した。会話・メッセージ・回答の記録・実行履歴は継続する。
新規の schema の正本は `RAG_QUERY_HISTORY` を含まない。過去の migration は変更せず、
`20261029_001_retire_query_history` が同表の全行と自有索引・制約を削除する。表の不存在は許容し、
他表の参照などのエラーは握りつぶさない。旧コードが保存しないことを確認してから、現在の schema 管理の
`initialize --allow-destructive` で明示的に適用する。必要な履歴がある環境は先に同表を export する。
`RAG_QUERY_HISTORY_*` の設定は新コードでは使用しない。配備時は不要になった行だけを削除する。
