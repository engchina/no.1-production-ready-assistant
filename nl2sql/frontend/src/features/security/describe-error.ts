import type { DescribeApiError } from "@production-ready/system-settings";

import { ApiError } from "@/lib/api";

/** 共有のユーザー管理・ロール管理画面へ、入力項目のエラーとエラーコードを渡す（#206）。 */
export const describeSecurityApiError: DescribeApiError = (error) =>
  error instanceof ApiError
    ? {
        message: error.message,
        code: error.errorCode,
        fieldErrors: error.fieldErrors.map(({ pointer, message }) => ({ pointer, message })),
      }
    : error instanceof Error
      ? { message: error.message }
      : undefined;
