// 第三者の部品のライセンスの原文を frontend の build の成果物に同梱する Vite の plugin（#3392）。
//
// 3 製品の frontend は、npm の package の JavaScript / CSS と @fontsource / @fontsource-variable の woff2 を bundle して
// ブラウザへ配信する（配布にあたる）。成果物の `licenses/` に次を出す。
//   - `licenses/fonts/<書体>-<ライセンス>.txt`: 製品の package.json の dependencies / devDependencies にある
//     @fontsource* の package の LICENSE（SIL Open Font License 1.1 は、書体を配布するときに著作権表示とライセンスの原文を
//     添えることを求める）。一覧は `licenses/fonts/README.txt`。原文は node_modules の package から読み、版を上げたときに
//     手で直さない。原文が見つからなければ build を止める。
//   - `licenses/javascript.md`: bundle に入った npm の package のライセンスの一覧と原文（Vite の `build.license`）。
//     製品の vite.config.ts が `build.license` を指定していれば、それを優先する。
//
// 使い方（製品の vite.config.ts）:
//   import { thirdPartyLicenses } from "@production-ready/ui/vite/third-party-licenses";
//   plugins: [react(), thirdPartyLicenses()]

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const FONT_PACKAGE = /^@fontsource(-variable)?\//;
const LICENSE_FILES = ["LICENSE", "LICENSE.txt", "LICENSE.md", "OFL.txt"];
export const LICENSES_DIR = "licenses";
export const FONT_LICENSES_DIR = `${LICENSES_DIR}/fonts`;
export const JAVASCRIPT_LICENSES_FILE = `${LICENSES_DIR}/javascript.md`;

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

/** root から上へ node_modules/<name> を探す（npm の hoist・workspace のどちらでも見つける）。 */
function findPackageDir(root, name) {
  let dir = path.resolve(root);
  for (;;) {
    const candidate = path.join(dir, "node_modules", ...name.split("/"));
    if (existsSync(path.join(candidate, "package.json"))) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** `OFL-1.1` → `OFL`、`Apache-2.0` → `Apache`（ファイル名に使う）。 */
function licenseTag(license) {
  const tag = String(license || "LICENSE").replace(/-\d+(\.\d+)*$/, "");
  return tag.replace(/[^A-Za-z0-9.-]+/g, "-");
}

/**
 * 製品の package.json の @fontsource* の依存と、その LICENSE の原文を集める。
 * @param {string} root 製品の frontend のディレクトリ（package.json の場所）
 */
export function collectFontLicenses(root) {
  const manifest = readJson(path.join(root, "package.json"));
  const names = Object.keys({ ...manifest.dependencies, ...manifest.devDependencies })
    .filter((name) => FONT_PACKAGE.test(name))
    .sort();
  return names.map((name) => {
    const dir = findPackageDir(root, name);
    if (!dir) {
      throw new Error(`[third-party-licenses] ${name} が node_modules にありません（npm ci を実行してください）`);
    }
    const pkg = readJson(path.join(dir, "package.json"));
    const licenseFile = LICENSE_FILES.map((file) => path.join(dir, file)).find((file) => existsSync(file));
    if (!licenseFile) {
      throw new Error(`[third-party-licenses] ${name} にライセンスの原文（${LICENSE_FILES.join(" / ")}）がありません`);
    }
    const base = name.split("/")[1];
    return {
      name,
      version: String(pkg.version ?? ""),
      license: String(pkg.license ?? ""),
      fileName: `${FONT_LICENSES_DIR}/${base}-${licenseTag(pkg.license)}.txt`,
      text: readFileSync(licenseFile, "utf8"),
    };
  });
}

/** `licenses/fonts/README.txt` の本文（同梱した書体と原文のファイルの一覧）。 */
export function fontLicensesIndex(items) {
  const lines = [
    "Third-party fonts bundled in this build (served to the browser as woff2 files).",
    "Each font is distributed under the license reproduced in the file listed below.",
    "",
    ...items.map((item) => `${item.name}@${item.version}  ${item.license}  ${path.posix.basename(item.fileName)}`),
    "",
  ];
  return lines.join("\n");
}

/** build（vite build）のときだけ、第三者の部品のライセンスの原文と一覧を成果物に出す。 */
export function thirdPartyLicenses() {
  let root = process.cwd();
  return {
    name: "production-ready-third-party-licenses",
    apply: "build",
    config(userConfig) {
      if (userConfig.build?.license !== undefined) return undefined;
      return { build: { license: { fileName: JAVASCRIPT_LICENSES_FILE } } };
    },
    configResolved(config) {
      root = config.root;
    },
    generateBundle() {
      const items = collectFontLicenses(root);
      if (items.length === 0) return;
      for (const item of items) {
        this.emitFile({ type: "asset", fileName: item.fileName, source: item.text });
      }
      this.emitFile({ type: "asset", fileName: `${FONT_LICENSES_DIR}/README.txt`, source: fontLicensesIndex(items) });
    },
  };
}
