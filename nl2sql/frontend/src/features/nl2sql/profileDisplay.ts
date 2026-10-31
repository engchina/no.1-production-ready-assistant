import type { SelectFieldOption } from "@production-ready/ui";

export interface ProfileDisplaySource {
  name?: string | null;
  category?: string | null;
}

export interface ProfileRecordDisplaySource {
  profile_name?: string | null;
  profile_category?: string | null;
}

export interface ProfileOptionSource extends ProfileDisplaySource {
  id: string;
}

function cleaned(value?: string | null) {
  return value?.trim() ?? "";
}

export function profileNameLabel(name?: string | null) {
  return cleaned(name) || "-";
}

export function profileCategoryLabel(category?: string | null) {
  return cleaned(category) || "-";
}

export function profileDisplayLabel(profile: ProfileDisplaySource) {
  const name = cleaned(profile.name);
  if (!name) return "-";
  const category = cleaned(profile.category);
  return category ? `${name}（${category}）` : name;
}

export function profileRecordDisplayLabel(profile: ProfileRecordDisplaySource) {
  return profileDisplayLabel({
    name: profile.profile_name,
    category: profile.profile_category,
  });
}

export function profileSelectOption(profile: ProfileOptionSource): SelectFieldOption {
  return {
    value: profile.id,
    label: profileNameLabel(profile.name),
    description: profileCategoryLabel(profile.category),
  };
}
