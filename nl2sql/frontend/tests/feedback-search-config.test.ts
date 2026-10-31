import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FEEDBACK_MATCH_LIMIT,
  DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD,
  feedbackSearchConfigOrDefault,
} from "../src/features/nl2sql/feedbackSearchConfig.ts";

test("類似検索インデックスの最低スコアの既定は 0.9、最大候補数の既定は 3（#1721）", () => {
  assert.equal(DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD, 0.9);
  assert.equal(DEFAULT_FEEDBACK_MATCH_LIMIT, 3);
  assert.deepEqual(feedbackSearchConfigOrDefault(null), { similarity_threshold: 0.9, match_limit: 3 });
  assert.deepEqual(feedbackSearchConfigOrDefault(undefined), { similarity_threshold: 0.9, match_limit: 3 });
});

test("保存済みの値は 0 を含めてそのまま使う", () => {
  assert.deepEqual(feedbackSearchConfigOrDefault({ similarity_threshold: 0, match_limit: 7 }), {
    similarity_threshold: 0,
    match_limit: 7,
  });
  assert.deepEqual(feedbackSearchConfigOrDefault({ similarity_threshold: 0.65, match_limit: 1 }), {
    similarity_threshold: 0.65,
    match_limit: 1,
  });
});
