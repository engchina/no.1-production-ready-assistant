import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export function runTestFile(file) {
  const env = { ...process.env };
  // 親が node:test の worker でも、子は独立した TAP の実行器として起動する。
  delete env.NODE_TEST_CONTEXT;
  // jiti の hook を使うプロセス自身で収集する。ファイル間の状態は子プロセスで分離する。
  const result = spawnSync(process.execPath, [
    "--import", "jiti/register", "--test", "--experimental-test-isolation=none",
    "--test-reporter=tap", file,
  ], { env, encoding: "utf8", timeout: 60_000, maxBuffer: 10 * 1024 * 1024 });
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  const tests = Number(result.stdout?.match(/^# tests (\d+)$/m)?.[1] ?? 0);
  const passed = Number(result.stdout?.match(/^# pass (\d+)$/m)?.[1] ?? 0);
  // Node は空のファイルも同名の成功 1 件として出すため、用例名も確認する。
  const names = [...(result.stdout ?? "").matchAll(/^# Subtest: (.+)$/gm)]
    .map((match) => match[1]);
  const hasCase = names.some((name) => resolve(name) !== resolve(file));
  const ok = !result.error && result.status === 0 && tests > 0 && passed > 0 && hasCase;
  return { ok, tests, passed, output, error: result.error };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const files = process.argv.slice(2);
  if (!files.length) {
    files.push(...readdirSync("tests").filter((file) => file.endsWith(".test.ts"))
      .sort().map((file) => `tests/${file}`));
  }
  if (!files.length) {
    console.error("テストファイルがありません。");
    process.exitCode = 1;
  }
  let total = 0;
  for (const file of files) {
    const result = runTestFile(file);
    process.stdout.write(result.output);
    if (!result.ok) {
      console.error(`${file}: テストが失敗したか、成功した具体的なテストケースがありません。`);
      if (result.error) console.error(result.error.message);
      process.exitCode = 1;
    }
    total += result.tests;
  }
  console.log(`具体的なテストケース: ${total} 件 / ${files.length} ファイル`);
}
