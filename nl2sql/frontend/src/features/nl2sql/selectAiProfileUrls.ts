const BUSINESS_SELECT_AI_PROFILE_FILTER =
  "business_profiles_only=true&include_archived_business_profiles=true";

export const BUSINESS_SELECT_AI_DB_PROFILES_URL =
  `/api/nl2sql/select-ai/db-profiles?${BUSINESS_SELECT_AI_PROFILE_FILTER}`;

export const BUSINESS_SELECT_AI_DB_PROFILES_DETAIL_URL =
  `/api/nl2sql/select-ai/db-profiles?include_detail=true&${BUSINESS_SELECT_AI_PROFILE_FILTER}`;
