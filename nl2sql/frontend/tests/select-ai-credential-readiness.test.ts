import assert from "node:assert/strict";
import test from "node:test";

import {
  enginesUseSelectAiCredential,
  engineUsesSelectAiCredential,
  isSelectAiCredentialMissing,
  isSelectAiCredentialMissingError,
} from "../src/features/nl2sql/selectAiCredentialReadiness.ts";
import { ApiError } from "../src/lib/api.ts";

// Select AI の Credential の事前条件（#1683 / #1688）。

test("Credential が要り、無いと確かめられたときだけ事前条件が足りない", () => {
  assert.equal(
    isSelectAiCredentialMissing({
      credential_required: true,
      credential_name: "NL2SQL_OCI_CRED",
      credential_exists: false,
    }),
    true,
  );
  assert.equal(
    isSelectAiCredentialMissing({
      credential_required: true,
      credential_name: "NL2SQL_OCI_CRED",
      credential_exists: true,
    }),
    false,
  );
  // 確かめられない（DB に接続できない）・不要（deterministic runtime）・未取得のときは止めない。
  assert.equal(
    isSelectAiCredentialMissing({
      credential_required: true,
      credential_name: "NL2SQL_OCI_CRED",
      credential_exists: null,
    }),
    false,
  );
  assert.equal(
    isSelectAiCredentialMissing({
      credential_required: false,
      credential_name: "",
      credential_exists: null,
    }),
    false,
  );
  assert.equal(isSelectAiCredentialMissing(undefined), false);
});

test("Select AI と Select AI Agent だけが Credential を使う", () => {
  assert.equal(engineUsesSelectAiCredential("select_ai"), true);
  assert.equal(engineUsesSelectAiCredential("select_ai_agent"), true);
  assert.equal(engineUsesSelectAiCredential("enterprise_ai_direct"), false);
  assert.equal(enginesUseSelectAiCredential(["enterprise_ai_direct", "select_ai_agent"]), true);
  assert.equal(enginesUseSelectAiCredential(["enterprise_ai_direct"]), false);
  assert.equal(enginesUseSelectAiCredential([]), false);
});

test("操作の開始の 409 SELECT_AI_CREDENTIAL_MISSING を見分ける", () => {
  assert.equal(
    isSelectAiCredentialMissingError(
      new ApiError(409, ["Select AI の Credential を先に作成してください"], "SELECT_AI_CREDENTIAL_MISSING"),
    ),
    true,
  );
  assert.equal(isSelectAiCredentialMissingError(new ApiError(409, ["競合"], "CONFLICT")), false);
  assert.equal(isSelectAiCredentialMissingError(new Error("SELECT_AI_CREDENTIAL_MISSING")), false);
});
