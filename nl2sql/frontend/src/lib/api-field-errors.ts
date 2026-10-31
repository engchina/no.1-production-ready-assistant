import { ApiError, type ApiFieldProblem } from "./api";

export type ApiFieldErrorMap<FieldName extends string> = Partial<Record<FieldName, string>>;

export function withoutFieldError<FieldName extends string>(
  errors: ApiFieldErrorMap<FieldName>,
  field: FieldName
): ApiFieldErrorMap<FieldName> {
  if (!errors[field]) return errors;
  const next = { ...errors };
  delete next[field];
  return next;
}

/** JSON Pointer を各画面が宣言した field 名へ結び付ける。表示文言の解析はしない。 */
export function mapApiFieldErrors<FieldName extends string>(
  cause: unknown,
  pointerToField: Readonly<Record<string, FieldName>>,
  messageFor?: (problem: ApiFieldProblem, cause: ApiError) => string
): ApiFieldErrorMap<FieldName> {
  if (!(cause instanceof ApiError)) return {};
  const mapped: ApiFieldErrorMap<FieldName> = {};
  for (const problem of cause.fieldErrors) {
    const fieldName = pointerToField[problem.pointer];
    if (!fieldName) continue;
    mapped[fieldName] = messageFor?.(problem, cause) ?? problem.message;
  }
  return mapped;
}

/** すべての問題が field に結び付いた場合は、重複する FormStatus を出さない。 */
export function unmappedApiErrorMessage<FieldName extends string>(
  cause: unknown,
  pointerToField: Readonly<Record<string, FieldName>>,
  fallback: string
): string {
  if (!(cause instanceof ApiError)) {
    return cause instanceof Error && cause.message.trim() ? cause.message : fallback;
  }
  const hasFieldErrors = cause.fieldErrors.length > 0;
  const hasUnmappedField = cause.fieldErrors.some(
    (problem) => pointerToField[problem.pointer] === undefined
  );
  return hasFieldErrors && !hasUnmappedField ? "" : cause.message || fallback;
}
