// third-party-licenses.mjs の型（製品の vite.config.ts と platform の tests が読む。#3392）。

export declare const LICENSES_DIR: "licenses";
export declare const FONT_LICENSES_DIR: "licenses/fonts";
export declare const JAVASCRIPT_LICENSES_FILE: "licenses/javascript.md";

export interface FontLicense {
  /** package の名前（例: `@fontsource-variable/noto-sans-jp`） */
  name: string;
  version: string;
  /** package.json の license（SPDX。例: `OFL-1.1`） */
  license: string;
  /** 成果物の中のパス（例: `licenses/fonts/noto-sans-jp-OFL.txt`） */
  fileName: string;
  /** ライセンスの原文 */
  text: string;
}

export declare function collectFontLicenses(root: string): FontLicense[];

export declare function fontLicensesIndex(items: FontLicense[]): string;

/** Vite の Plugin（vite の型に依存しないよう、使う hook だけを書く）。 */
export declare function thirdPartyLicenses(): {
  name: string;
  apply: "build";
  config(userConfig: { build?: { license?: unknown } }): { build: { license: { fileName: string } } } | undefined;
  configResolved(config: { root: string }): void;
  generateBundle(this: { emitFile(file: { type: "asset"; fileName: string; source: string }): string }): void;
};
