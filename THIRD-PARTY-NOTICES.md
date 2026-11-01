# 第三者の部品の通知（THIRD-PARTY NOTICES）

No.1 Production Ready Suite（`platform/`・`rag/`・`nl2sql/`・`agent/`）が使う第三者の部品のうち、許諾の条件に表示・原文の同梱・ソースの提供などの義務があるもの、または permissive（MIT・BSD・Apache-2.0 など）でないものの一覧です（#3392）。自前のコードのライセンスは [LICENSE](./LICENSE)（MIT）です。

> **これは法的な助言ではありません。** 技術の調査（lockfile・package の metadata・wheel に同梱されたライセンスのファイル）の結果であり、利用・納品・再配布の形に合わせた最終の判断は、法務・知財の担当者が行ってください。各部品のライセンスの正本は、その部品の配布物に含まれるライセンスのファイルです。

3 製品は 1 台の Compute にまとめて配備し、platform の共通基盤と多くの依存を共有するため、通知は製品ごとに分けずにこの 1 つのファイルに置き、表の「場所」の列で製品・lockfile を示します。

## 入手の経路と再配布

この repo は、自前のソースと配備のスクリプトだけを含み、下の表の第三者の binary を含みません。配備のスクリプトは、配備先の Compute で次のとおり第三者の部品を取得します。

| 種類 | 取得の仕方 | 取得元 |
|---|---|---|
| Python の package | 各 backend・前処理・parser のサービスのディレクトリで `uv sync --locked --no-dev`（`uv.lock` の版と hash で固定。任意の extra は入れない） | PyPI（`torch` は PyTorch の CPU の index） |
| npm の package | 各製品の `frontend/` で `npm ci` と `npm run build`（`package-lock.json` で固定） | npm registry |
| OS の package | `dnf install`（`platform/deploy/suite-init.sh`・`rag/init_script.sh`） | Oracle Linux 9 の BaseOS / AppStream |
| uv・Python・Node.js | uv は Astral の install script、Python は `uv python install`、Node.js は AppStream の module（無ければ nodejs.org の配布） | 各配布元 |

- 配備先で取得する限り、第三者の binary の再配布者は、それを取得した各環境の管理者です（この repo の開発者が配るのではありません）。
- **次の場合は再配布にあたります。** 第三者の部品の条件（ソースの提供・原文の同梱・改変の許可など）を、配る側が満たしてください: 構築済みの venv・`node_modules`・OS の image・オフラインの bundle を渡す、Compute の image を作って配る、顧客の環境へ構築済みの一式を納品する。
- **frontend の成果物（`dist/`）はブラウザへ配信する時点で配布にあたります。** そのため build のときに、成果物の `licenses/` に第三者のライセンスを出します（platform の共有の Vite の plugin `@production-ready/ui/vite/third-party-licenses`。3 製品の `vite.config.ts` で使う）。
  - `licenses/javascript.md`: bundle に入った npm の package の名前・版・ライセンスと原文（Vite の `build.license`）。
  - `licenses/fonts/<書体>-OFL.txt` と `licenses/fonts/README.txt`: 自前で配信する書体の SIL Open Font License 1.1 の原文と一覧（下の「書体」）。
- LGPL の部品を含む一式を納品するときは、利用者がその部品を差し替えられることを妨げない（LGPL-2.1 §6 / LGPL-3.0 §4 に反して、改変・デバッグのための reverse engineering を禁じる条項を EULA に置かない）ようにしてください。

## 強い copyleft・制限のある部品

なし。以前の `rag/backend` は PDF の読み取りに `pymupdf`（AGPL-3.0 または Artifex の商用ライセンス）を使っていたが、#3343 で `pypdfium2`（下の「PDFium」）と Pillow に置き換え、依存から外した。

## 弱い copyleft の部品（同梱のライブラリを含む）

| 部品 | 取得元 | ライセンス | 場所 | 使い方と注記 |
|---|---|---|---|---|
| `opencv-python-headless` | PyPI | Apache-2.0。wheel の `opencv_python_headless.libs` に FFmpeg（libavcodec・libavformat・libavutil・libswscale・libswresample。LGPL-2.1-or-later）、libvpx・libaom・libavif（BSD 系）、OpenBLAS（BSD-3-Clause）、GCC runtime（`libgfortran`・`libquadmath`。下の numpy と同じ）を同梱 | `rag/services/parsers/docling`・`rag/services/parsers/unstructured` | 画像の処理に使う。同梱の部品のライセンスは wheel の `LICENSE-3RD-PARTY.txt` |
| `opencv-python` | PyPI | Apache-2.0。FFmpeg（LGPL-2.1-or-later）と Qt（LGPL-3.0）を同梱 | `rag/backend` と `rag/packages/rag_engine` の任意の extra `docling` だけ | 配備は extra を入れないので、配備先には入らない（開発の環境だけ） |
| `shapely` | PyPI | BSD-3-Clause。wheel の `shapely.libs` に GEOS（`libgeos`・`libgeos_c`。LGPL-2.1）を同梱 | `rag/services/parsers/docling`（RapidOCR の依存）、`rag/backend` の extra `docling` | GEOS のライセンスは wheel の `LICENSE_GEOS` |
| `numpy`・`scipy` | PyPI | BSD-3-Clause。wheel の `numpy.libs`・`scipy.libs` に OpenBLAS（BSD-3-Clause）、LAPACK（BSD-3-Clause-Open-MPI）、`libgfortran`（GPL-3.0-or-later WITH GCC-exception-3.1）、`libquadmath`（LGPL-2.1-or-later）を同梱 | `nl2sql/backend`・`rag/backend`・`rag/services/parsers/docling`・`rag/services/parsers/unstructured` ほか | 同梱の部品のライセンスは wheel の `LICENSE.txt`（`numpy-*.dist-info/licenses/`） |
| `torch`・`torchvision` | PyPI | BSD-3-Clause。`torch/lib` に `libgomp`（GPL-3.0-or-later WITH GCC-exception-3.1）を同梱 | `rag/services/parsers/docling`・`rag/services/parsers/unstructured`（CPU 版） | GCC Runtime Library Exception により、利用する側のライセンスに GPL は及ばない |
| `pikepdf` | PyPI | MPL-2.0。wheel の `pikepdf.libs` に qpdf（Apache-2.0）と libjpeg（IJG）を同梱 | `rag/services/parsers/unstructured`、`rag/backend` の extra `unstructured` | MPL-2.0 はファイル単位の copyleft。改変しない |
| `certifi` | PyPI | MPL-2.0 | すべての backend・サービス | Mozilla の CA の bundle。改変しない |
| `tqdm` | PyPI | MPL-2.0 AND MIT | `rag/` のすべての venv | 改変しない |
| `@fontsource-variable/noto-sans-jp`・`@fontsource-variable/noto-sans-sc`・`@fontsource-variable/noto-sans-tc`・`@fontsource-variable/noto-sans-hk`・`@fontsource/roboto`・`@fontsource/google-sans-code` | npm | OFL-1.1 | 3 製品の `frontend/` | 下の「書体」 |

## build・test だけで使う部品（成果物に含まない）

| 部品 | 取得元 | ライセンス | 場所 | 使い方と注記 |
|---|---|---|---|---|
| `lightningcss`・`lightningcss-*` | npm | MPL-2.0 | 3 製品の `frontend/`・`platform/`（Vite・Tailwind の CSS の build） | build の道具。成果物には含まれない |
| `axe-core`・`@axe-core/playwright` | npm | MPL-2.0 | 3 製品の `frontend/`（e2e のアクセシビリティの検査） | test だけ |
| `pathspec` | PyPI | MPL-2.0 | backend の dev の依存（`mypy`） | 型検査だけ |

Playwright（Apache-2.0。NOTICE あり）も e2e だけで使い、成果物に含みません。

## PDFium（`pypdfium2` の同梱）

`pypdfium2`（Apache-2.0 / BSD-3-Clause。wheel の metadata は `BSD-3-Clause, Apache-2.0, dependency licenses`）は `rag/backend`・`rag/packages/rag_engine`（extra `pdf`・`docling`）・`nl2sql/backend`（オントロジーの資料の読み取り）・`rag/services/parsers/docling`・`rag/services/parsers/unstructured` の依存です。wheel は PDFium の build（pdfium-binaries）の `libpdfium.so` を同梱し、その build に含まれる部品のライセンスを wheel の `pypdfium2-*.dist-info/licenses/data/<platform>/BUILD_LICENSES/` に置いています（下の一覧は `pypdfium2` 5.14.0 の linux_x64 の wheel で確かめた）。

| 部品 | 取得元 | ライセンス | 注記 |
|---|---|---|---|
| `pypdfium2` | PyPI | Apache-2.0 / BSD-3-Clause（自身）、CC-BY-4.0（一部のファイル） | |
| PDFium | 同梱 | BSD-3-Clause | `pdfium.txt` |
| pdfium-binaries | 同梱 | MIT | `pdfium-binaries.txt` |
| FreeType | 同梱 | FreeType License（FTL）と GPL-2.0 の選択。ここでは FTL を選ぶ | `freetype.txt`。FTL は、binary を配るときに文書で FreeType を使っていることを示すことを求める（下の表示） |
| ICU | 同梱 | Unicode License V3 | `icu.txt` |
| libjpeg-turbo | 同梱 | IJG License・BSD-3-Clause・Zlib | `libjpeg_turbo.md`・`libjpeg_turbo.ijg`。IJG は、binary だけを配るときに文書で IJG の成果に基づくことを示すことを求める（下の表示） |
| libpng | 同梱 | libpng License | `libpng.txt` |
| zlib | 同梱 | Zlib | `zlib.txt` |
| OpenJPEG | 同梱 | BSD-2-Clause | `libopenjpeg.txt` |
| Little CMS（lcms） | 同梱 | MIT | `lcms.txt` |
| Anti-Grain Geometry 2.3（AGG） | 同梱 | AGG の許諾（permissive） | `agg23.txt` |
| Abseil | 同梱 | Apache-2.0 | `abseil.txt` |
| LLVM libc | 同梱 | Apache-2.0 WITH LLVM-exception | `llvm-libc.txt` |
| fast_float | 同梱 | MIT（fast_float は Apache-2.0 / MIT / BSL-1.0 の選択。同梱のファイルは MIT） | `fast_float.txt` |
| simdutf | 同梱 | MIT（simdutf は Apache-2.0 / MIT の選択。同梱のファイルは MIT） | `simdutf.txt` |

前処理・parser の wheel（`pypdfium2` 5.10.1）の `BUILD_LICENSES` には LLVM libc・simdutf が無く、ほかは同じです。

**表示（FreeType・IJG の求める文書での表示）**

- Portions of this software are copyright © 2025 The FreeType Project (www.freetype.org). All rights reserved.（年は PDFium の build が含む FreeType の版の年。pypdfium2 5.14.0（PDFium 156.0.8076）の FreeType の版は未確認のため、版を確かめて直す）
- This software is based in part on the work of the Independent JPEG Group.

## 書体

3 製品の frontend は、次の書体を npm の `@fontsource` / `@fontsource-variable` の package から bundle し、自前で配信します（実行時に外部の CDN を使わない）。いずれも SIL Open Font License 1.1（OFL-1.1）です。

| 書体 | package | 使い方 |
|---|---|---|
| Noto Sans JP（可変） | `@fontsource-variable/noto-sans-jp` | 本文（日本語・ラテン文字） |
| Noto Sans SC / TC / HK（可変） | `@fontsource-variable/noto-sans-sc`・`-tc`・`-hk` | Noto Sans JP に無い簡体字・繁体字・香港の字 |
| Roboto | `@fontsource/roboto` | ラテン文字の fallback |
| Google Sans Code | `@fontsource/google-sans-code` | 等幅（ID・ログ・SQL） |

OFL-1.1 は、書体を配布するときに著作権表示とライセンスの原文を添えることを求めます。3 製品の `npm run build` は、使っている書体の package の `LICENSE` を `dist/licenses/fonts/<書体>-OFL.txt` に、一覧を `dist/licenses/fonts/README.txt` に出します（package から読むので、版を上げたときに原文を手で直さない）。`dist/` を配信すると、これも `/<製品>/licenses/fonts/` で配信されます。書体を改変（subset の再生成など）して配るときは、OFL の Reserved Font Name の条件を確かめてください。

## Apache-2.0 の NOTICE

Apache License 2.0 §4(d) は、NOTICE のファイルを持つ部品を再配布するときに、その帰属の表示を添えることを求めます。実行時の依存のうち NOTICE を持つ Python の package と、その本文（各 package の `*.dist-info/licenses/` から写した）を次に集めます。上の「入手の経路」のとおり、配備先で PyPI から取得する限り、NOTICE は各 package とともに venv の中にあります。

| 部品 | 取得元 | ライセンス | 場所 | NOTICE を写した版 |
|---|---|---|---|---|
| `aiofiles` | PyPI | Apache-2.0 | `rag/backend`・`rag/services/parsers/unstructured` | 25.1.0 |
| `langdetect` | PyPI | Apache-2.0 | `rag/backend`・`rag/services/parsers/unstructured` | 1.0.9 |
| `mail-parser` | PyPI | Apache-2.0 | `rag/backend`・`rag/services/parsers/docling` | 4.4.0 |
| `msoffcrypto-tool` | PyPI | MIT（NOTICE に BSD-3-Clause の派生の表示） | `rag/backend`・`rag/services/parsers/unstructured` | 6.0.0 |
| `onnx` | PyPI | Apache-2.0 | `rag/backend`・`rag/services/parsers/unstructured` | 1.22.0 |
| `oracledb` | PyPI | UPL-1.0 OR Apache-2.0 | `agent/backend`・`nl2sql/backend`・`rag/backend` | 4.0.2 |
| `prometheus-client` | PyPI | Apache-2.0 AND BSD-2-Clause | すべての backend・サービス | 0.26.0 |
| `requests` | PyPI | Apache-2.0 | すべての backend・サービス | 2.34.2 |

### aiofiles 25.1.0

```text
Asyncio support for files
Copyright 2016 Tin Tvrtkovic
```

### langdetect 1.0.9

```text
language-detection license
==========================

    Copyright (c) 2010-2014 Cybozu Labs, Inc. All rights reserved.

    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific language governing permissions and limitations under the License.
```

### mail-parser 4.4.0

```text
Copyright 2016 Fedele Mantuano (https://twitter.com/fedelemantuano)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

### msoffcrypto-tool 6.0.0

```text
This software contains derivative works from https://github.com/herumi/msoffice
which is licensed under the BSD 3-Clause License.

https://github.com/herumi/msoffice/blob/c3cdb1ea0a5285a2a1718fee2dc893fd884bdad0/COPYRIGHT

Copyright (c) 2007-2015 Cybozu Labs, Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

Redistributions of source code must retain the above copyright notice, this
list of conditions and the following disclaimer.
Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.
Neither the name of the Cybozu Labs, Inc. nor the names of its contributors may
be used to endorse or promote products derived from this software without
specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
THE POSSIBILITY OF SUCH DAMAGE.
```

### onnx 1.22.0

```text
ONNX is licensed under the Apache License, Version 2.0 (see LICENSE).

This product includes software from the following third-party projects, which
are linked into ONNX wheel builds. Their copyright notices are reproduced below.

---

## abseil-cpp

Copyright 2017 The Abseil Authors.

Version: 20250127.0
Source: https://github.com/abseil/abseil-cpp
License: Apache-2.0 (see LICENSE)

---

## protobuf

Copyright 2008 Google Inc.

Version: 25.1
Source: https://github.com/protocolbuffers/protobuf
License: Apache-2.0 (see LICENSE)

---

## nanobind

Copyright (c) 2022 Wenzel Jakob <wenzel.jakob@epfl.ch>

Version: 2.12.0
Source: https://github.com/wjakob/nanobind
License: BSD-3-Clause

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### oracledb 4.0.2

```text
Copyright (c) 2016, 2026, Oracle and/or its affiliates.
```

### prometheus-client 0.26.0

```text
Prometheus instrumentation library for Python applications
Copyright 2015 The Prometheus Authors

This product bundles decorator 4.0.10 which is available under a "2-clause BSD"
license. For details, see prometheus_client/decorator.py.
```

### requests 2.34.2

```text
Requests
Copyright 2019 Kenneth Reitz
```

`onnxruntime`（MIT。`rag/backend`・`rag/services/parsers/docling`・`rag/services/parsers/unstructured`）は、同梱する第三者の部品の通知を package の `onnxruntime/ThirdPartyNotices.txt` に置いています（約 5,000 行のためここには写しません）。

## OS の repo から入れる package（再配布しない）

配備のスクリプトが Compute で Oracle Linux 9 の repo から `dnf` で入れる package です。OS の配布物として入れるもので、この repo・release には含まず、再配布しません。各 package のライセンスは `rpm -q --qf '%{LICENSE}\n' <package>` で確かめられます。

| package | 入れるもの | ライセンス（Oracle Linux の package の記載の要約） |
|---|---|---|
| `libreoffice-core`・`libreoffice-writer`・`libreoffice-impress`・`libreoffice-calc` | RAG の前処理（Office → PDF、`.doc` → `.docx`）。別の process で起動する | (MPLv1.1 or LGPLv3+) and LGPLv3 and LGPLv2+ and BSD and MPLv2.0 ほか |
| `poppler-utils` | RAG の parser（`parser-unstructured` の PDF の画像化） | (GPLv2 or GPLv3) and GPLv2+ and LGPLv2+ and MIT。別の process のコマンドとして起動する |
| `tesseract`・`tesseract-langpack-eng`・`tesseract-langpack-jpn` | RAG の parser の OCR | ASL 2.0（Apache-2.0） |
| `google-noto-sans-cjk-ttc-fonts`・`google-noto-serif-cjk-ttc-fonts` | LibreOffice の PDF 化の書体 | OFL（OFL-1.1） |
| `git`・`openssl`・`policycoreutils-python-utils`・`gcc`・`gcc-c++`・`make` ほか | 配備の道具（`SUITE_OS_PACKAGES`・`BASE_SYSTEM_PACKAGES`） | 各 package の記載のとおり |

## 任意の部品（配備は入れない）

| 部品 | ライセンス | 注記 |
|---|---|---|
| Oracle Instant Client | Oracle Free Use Terms and Conditions | python-oracledb の Thick mode を使うときだけ、利用者が Oracle から取得して `PLATFORM_ORACLE_CLIENT_LIB_DIR` に置く。既定の Thin mode では要らない。配備のスクリプトは入れない |

## repo の中の第三者の内容

| 場所 | ライセンス | 注記 |
|---|---|---|
| `nl2sql/backend/app/features/nl2sql/prompts/sql_assist/` | Apache-2.0 | 外部の repo のプロンプトの文を元にした部分。原文の `LICENSE` と、出典・変更点を書いた `NOTICE` を同じディレクトリに置く |

## 一覧の更新の手順

- **lockfile との突き合わせ**: `python3 scripts/third-party-notices.py check` が、この文書の表（1 列目が `名前`、2 列目が `PyPI` / `npm` の行）と、git が管理する `uv.lock` / `package-lock.json` を突き合わせます。表の部品が依存から外れたとき、npm の package（dev を含む）に permissive でないライセンスが増えて表に無いときに失敗します（CI の `Suite / Scripts`）。
- **依存の一覧**: `python3 scripts/third-party-notices.py inventory > ./tmp/third-party-notices/inventory.csv` が、lockfile から辿った依存（Python は `dependencies` と extra、npm は lockfile のライセンスつき）を CSV で出します。
- **Python のライセンス**: `uv.lock` はライセンスを持たないため、依存を足した・版を上げたときは、その venv の metadata で確かめます（例: `uv run --no-sync python -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License')) for d in m.distributions()]"`）。同梱の native のライブラリは `<package>.libs/`・`*.dist-info/licenses/` を見ます。permissive でないもの・NOTICE を持つものが増えたら、この文書に足します。
- 同じ PR で lockfile とこの文書をそろえます。
