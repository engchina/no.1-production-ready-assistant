import { cpSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// tokens.css（エントリ）と tokens/ 配下は Tailwind v4 のデザイントークン定義。バンドルせず素の CSS として
// dist へ配布し、各アプリの globals.css が `@import "@production-ready/ui/tokens.css"` で取り込む
// （tokens.css 内の相対 @import はアプリ側の Tailwind が解決する）。
const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../src/styles");
const dest = resolve(here, "../dist");

cpSync(src, dest, { recursive: true });
console.log(`[copy-css] ${src} -> ${dest}`);
