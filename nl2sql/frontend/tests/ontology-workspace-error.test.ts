import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  classifyOntologyWorkspaceError,
  ontologyWorkspaceErrorPresentation,
} from "../src/features/nl2sql/ontologyWorkspaceError.ts";
import { ApiError } from "../src/lib/api.ts";

function timeoutError(): Error {
  const error = new Error("request timed out");
  error.name = "TimeoutError";
  return error;
}

test("Ontology View の timeout を専用の日本語メッセージへ変換する", () => {
  const failure = classifyOntologyWorkspaceError(null, timeoutError());
  const i18nSource = readFileSync(
    new URL("../src/lib/i18n.ts", import.meta.url),
    "utf8"
  );

  assert.deepEqual(failure, { source: "ontology", kind: "timeout" });
  assert.deepEqual(failure ? ontologyWorkspaceErrorPresentation(failure) : null, {
    key: "ontologyBuild.workspace.timeout",
  });
  assert.ok(
    i18nSource.includes(
      "オントロジーの読み込みがタイムアウトしました。接続状態を確認して再試行してください。"
    )
  );
});

test("Profile 詳細の失敗を Ontology View の失敗と区別する", () => {
  const failure = classifyOntologyWorkspaceError(new Error("profile failure"), null);

  assert.deepEqual(failure, { source: "profile", kind: "unknown" });
  assert.deepEqual(failure ? ontologyWorkspaceErrorPresentation(failure) : null, {
    key: "ontologyBuild.workspace.profileError",
  });
});

test("ApiError の公開メッセージを原因として表示する", () => {
  const failure = classifyOntologyWorkspaceError(
    null,
    new ApiError(503, ["プロファイル範囲の準備に失敗しました。"])
  );

  assert.deepEqual(failure, {
    source: "ontology",
    kind: "api",
    publicMessage: "プロファイル範囲の準備に失敗しました。",
  });
  assert.deepEqual(failure ? ontologyWorkspaceErrorPresentation(failure) : null, {
    key: "ontologyBuild.workspace.apiError",
    params: { message: "プロファイル範囲の準備に失敗しました。" },
  });
});

test("両方が失敗した場合は先に必要な Profile 詳細の原因を優先する", () => {
  const failure = classifyOntologyWorkspaceError(
    new ApiError(404, ["対象プロファイルが見つかりません。"]),
    timeoutError()
  );

  assert.equal(failure?.source, "profile");
  assert.equal(failure?.publicMessage, "対象プロファイルが見つかりません。");
});
