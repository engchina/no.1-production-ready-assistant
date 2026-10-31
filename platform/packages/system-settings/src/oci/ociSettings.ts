export const FIXED_OCI_CONFIG_FILE = "~/.oci/config";
export const FIXED_OCI_CONFIG_PROFILE = "DEFAULT";
export const FIXED_OCI_KEY_FILE = "~/.oci/oci_api_key.pem";

export interface OciSettingsDraft {
  configFile: string;
  configProfile: string;
  userOcid: string;
  fingerprint: string;
  tenancyOcid: string;
  keyFile: string;
  region: string;
  objectStorageRegion: string;
  objectStorageNamespace: string;
}

export type OciSettingsField = keyof OciSettingsDraft;

export type OciValidationCode =
  | "required"
  | "invalid_fingerprint"
  | "invalid_tenancy_ocid"
  | "invalid_user_ocid"
  | "invalid_profile";

export type OciValidationResult = Partial<Record<OciSettingsField, OciValidationCode>>;

export const DEFAULT_OCI_SETTINGS: OciSettingsDraft = {
  configFile: FIXED_OCI_CONFIG_FILE,
  configProfile: FIXED_OCI_CONFIG_PROFILE,
  userOcid: "",
  fingerprint: "",
  tenancyOcid: "",
  keyFile: FIXED_OCI_KEY_FILE,
  region: "",
  objectStorageRegion: "",
  objectStorageNamespace: "",
};

export const REQUIRED_OCI_SETTINGS_FIELDS = [
  "configFile",
  "configProfile",
  "userOcid",
  "fingerprint",
  "tenancyOcid",
  "keyFile",
  "region",
  "objectStorageRegion",
  "objectStorageNamespace",
] as const satisfies readonly OciSettingsField[];

const OCI_FINGERPRINT_PATTERN = /^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2})+$/;
export function normalizeOciSettingsDraft(input: Partial<OciSettingsDraft>): OciSettingsDraft {
  return {
    configFile: FIXED_OCI_CONFIG_FILE,
    configProfile: FIXED_OCI_CONFIG_PROFILE,
    userOcid: (input.userOcid ?? DEFAULT_OCI_SETTINGS.userOcid).trim(),
    fingerprint: (input.fingerprint ?? DEFAULT_OCI_SETTINGS.fingerprint).trim(),
    tenancyOcid: (input.tenancyOcid ?? DEFAULT_OCI_SETTINGS.tenancyOcid).trim(),
    keyFile: FIXED_OCI_KEY_FILE,
    region: (input.region ?? DEFAULT_OCI_SETTINGS.region).trim(),
    objectStorageRegion: (
      input.objectStorageRegion ?? DEFAULT_OCI_SETTINGS.objectStorageRegion
    ).trim(),
    objectStorageNamespace: (
      input.objectStorageNamespace ?? DEFAULT_OCI_SETTINGS.objectStorageNamespace
    ).trim(),
  };
}

export function validateOciSettingsDraft(draft: OciSettingsDraft): OciValidationResult {
  const normalized = normalizeOciSettingsDraft(draft);
  const errors: OciValidationResult = {};

  for (const field of REQUIRED_OCI_SETTINGS_FIELDS) {
    if (!normalized[field]) {
      errors[field] = "required";
    }
  }

  if (normalized.userOcid && !normalized.userOcid.startsWith("ocid1.user.")) {
    errors.userOcid = "invalid_user_ocid";
  }

  if (normalized.tenancyOcid && !normalized.tenancyOcid.startsWith("ocid1.tenancy.")) {
    errors.tenancyOcid = "invalid_tenancy_ocid";
  }

  if (
    normalized.fingerprint &&
    !OCI_FINGERPRINT_PATTERN.test(normalized.fingerprint)
  ) {
    errors.fingerprint = "invalid_fingerprint";
  }

  return errors;
}
