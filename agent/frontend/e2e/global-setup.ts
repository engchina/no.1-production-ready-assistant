import type { FullConfig } from "@playwright/test";
import { stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const HERMETIC_MARKER = "e2e unmocked API";

// 実 backend が書き込む、利用者の実環境の設定ファイル（platform の pr_system_settings が書く
// OCI 設定・秘密鍵、3製品共通の platform/.env と model-settings.json（#211）、Agent の backend/.env）。
const backendDir = fileURLToPath(new URL("../../backend/", import.meta.url));
const platformDir = fileURLToPath(new URL("../../../platform/", import.meta.url));
const PROTECTED_FILES = [
  join(homedir(), ".oci", "config"),
  join(homedir(), ".oci", "oci_api_key.pem"),
  join(platformDir, ".env"),
  join(platformDir, "model-settings.json"),
  join(backendDir, ".env"),
  join(backendDir, "model-settings.json"),
];

async function fileFingerprint(path: string): Promise<string> {
  try {
    const info = await stat(path);
    return `${info.mtimeMs}:${info.size}`;
  } catch {
    return "missing";
  }
}

async function assertHermeticApi(baseURL: string) {
  // GET だけで確認する（hermetic でなかった場合に実 backend の状態を変えないため）。
  const probeUrl = new URL("/api/health", baseURL);
  let response: Response;
  try {
    response = await fetch(probeUrl);
  } catch (error) {
    throw new Error(`e2e の dev サーバへ接続できません: ${probeUrl}`, { cause: error });
  }
  const status = response.status;
  const body = await response.text();
  if (status !== 404 || !body.includes(HERMETIC_MARKER)) {
    throw new Error(
      [
        "e2e の dev サーバが hermetic ではありません。未モックの /api が実 backend へ届く可能性があるため、テストを開始しません。",
        `GET ${probeUrl} -> ${status} ${body.slice(0, 200)}`,
        "PLAYWRIGHT_HERMETIC_API=1 で起動した dev サーバ（playwright.config.ts の webServer）を使ってください。",
      ].join("\n")
    );
  }
}

export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]?.use.baseURL;
  if (!baseURL) {
    throw new Error("playwright.config.ts の use.baseURL が未設定です。");
  }
  await assertHermeticApi(baseURL);

  const before = new Map<string, string>();
  for (const path of PROTECTED_FILES) {
    before.set(path, await fileFingerprint(path));
  }

  return async () => {
    const changed: string[] = [];
    for (const [path, fingerprint] of before) {
      if ((await fileFingerprint(path)) !== fingerprint) {
        changed.push(path);
      }
    }
    if (changed.length > 0) {
      throw new Error(
        `e2e の実行中に利用者の実環境の設定ファイルが変更されました（mtime / size）: ${changed.join(", ")}`
      );
    }
  };
}
