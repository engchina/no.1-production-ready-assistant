import { compareNames } from "@production-ready/ui";

import type { Nl2SqlProfile, ProfileSummary, ProfileSummaryPage } from "./types";

export type ProfileListSortKey = "name" | "tables" | "views";
export type ProfileListSortDirection = "asc" | "desc";

export interface ProfileListSortState {
  key: ProfileListSortKey;
  direction: ProfileListSortDirection;
}

export function profileSummary(profile: Nl2SqlProfile): ProfileSummary {
  return {
    id: profile.id,
    name: profile.name,
    category: profile.category ?? "",
    description: profile.description,
    archived: profile.archived,
    allowed_table_count: profile.allowed_tables.length,
    allowed_view_count: profile.allowed_views.length,
    glossary_count: Object.keys(profile.glossary).length,
    few_shot_count: profile.few_shot_examples.length,
    version: profile.version ?? 1,
    etag: profile.etag ?? "",
    updated_at: profile.updated_at ?? "",
  };
}

export function profileSummaryPageFromLegacyList(
  profiles: Nl2SqlProfile[],
  query: string,
  sort: ProfileListSortState = { key: "name", direction: "asc" }
): ProfileSummaryPage {
  const normalizedQuery = query.trim().toLowerCase();
  const items = profiles
    .filter(
      (profile) =>
        !profile.archived &&
        (!normalizedQuery ||
          profile.name.toLowerCase().includes(normalizedQuery) ||
          (profile.category ?? "").toLowerCase().includes(normalizedQuery))
    )
    .map(profileSummary);
  // legacy 一覧 API は 1 ページで全件返るため、ここでの並べ替えは全体順になる。
  return {
    items: sortProfileSummariesForDisplay(items, sort),
    next_cursor: null,
    total: items.length,
    change_token: 0,
  };
}

function profileSortValue(profile: ProfileSummary, key: ProfileListSortKey) {
  if (key === "tables") return profile.allowed_table_count;
  if (key === "views") return profile.allowed_view_count;
  return profile.name;
}

export function sortProfileSummariesForDisplay(
  profiles: readonly ProfileSummary[],
  sort: ProfileListSortState
) {
  return [...profiles].sort((left, right) => {
    const a = profileSortValue(left, sort.key);
    const b = profileSortValue(right, sort.key);
    if (typeof a === "string" && typeof b === "string") return compareNames(a, b, sort.direction);
    const result = a < b ? -1 : a > b ? 1 : 0;
    return sort.direction === "asc" ? result : -result;
  });
}
