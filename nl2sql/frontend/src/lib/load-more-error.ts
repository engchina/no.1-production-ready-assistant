import { loadMoreErrorMessage } from "@production-ready/ui";

import { t } from "./i18n";
import { API_TIMEOUT_MS, requestTimeoutSeconds } from "./requestPolicy";

/**
 * 追加読み込み型の一覧の「さらに読み込む」の失敗の文言（#1266。各画面にあった同じ関数を 1 つにした）。
 * 待ち時間の上限は一覧の API の timeout（`API_TIMEOUT_MS.interactiveList`）の秒数を添え、ほかは例外の message、
 * message が無ければ `fallbackKey` の文言。
 */
export function listLoadMoreErrorMessage(error: unknown, fallbackKey: Parameters<typeof t>[0]) {
  return loadMoreErrorMessage(error, {
    timeoutMessage: t("objectSelector.loadMoreTimeout", {
      seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveList),
    }),
    fallback: t(fallbackKey),
  });
}
