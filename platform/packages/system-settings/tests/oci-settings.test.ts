import { describe, expect, it } from "vitest";

import {
  DEFAULT_OCI_SETTINGS,
  normalizeOciSettingsDraft,
  validateOciSettingsDraft,
  type OciSettingsDraft,
} from "../src/oci/ociSettings";
import { t } from "../src/oci/messages";

const COMPLETE_SETTINGS: OciSettingsDraft = {
  ...DEFAULT_OCI_SETTINGS,
  userOcid: "ocid1.user.oc1..example",
  fingerprint: "12:34:56:78:90:ab:cd:ef",
  tenancyOcid: "ocid1.tenancy.oc1..example",
  keyFile: "~/.oci/oci_api_key.pem",
  objectStorageNamespace: "mytenancy",
};

describe("normalizeOciSettingsDraft", () => {
  it("既定値を補完し、文字列値を trim する", () => {
    const draft = normalizeOciSettingsDraft({
      configFile: " /opt/oci/config ",
      configProfile: " PROD ",
      keyFile: " /home/app/.oci/prod.pem ",
      objectStorageNamespace: " mytenancy ",
    });

    expect(draft.configFile).toBe(DEFAULT_OCI_SETTINGS.configFile);
    expect(draft.configProfile).toBe("DEFAULT");
    expect(draft.keyFile).toBe(DEFAULT_OCI_SETTINGS.keyFile);
    expect(draft.region).toBe("");
    expect(draft.objectStorageRegion).toBe("");
    expect(draft.objectStorageNamespace).toBe("mytenancy");
  });
});

describe("validateOciSettingsDraft", () => {
  it("必須値の欠落を検出する", () => {
    const errors = validateOciSettingsDraft(DEFAULT_OCI_SETTINGS);

    expect(errors.userOcid).toBe("required");
    expect(errors.fingerprint).toBe("required");
    expect(errors.tenancyOcid).toBe("required");
    expect(errors.objectStorageNamespace).toBe("required");
  });

  it("OCI config 値の形式を検証する", () => {
    const errors = validateOciSettingsDraft({
      ...COMPLETE_SETTINGS,
      userOcid: "ocid1.tenancy.oc1..wrong",
      fingerprint: "not-a-fingerprint",
      tenancyOcid: "ocid1.user.oc1..wrong",
    });

    expect(errors.userOcid).toBe("invalid_user_ocid");
    expect(errors.fingerprint).toBe("invalid_fingerprint");
    expect(errors.tenancyOcid).toBe("invalid_tenancy_ocid");
  });
});

describe("OCI 画面の文言", () => {
  it("接続テストの全段階と状態に既定の文言がある（動的 key の抜け漏れ防止）", () => {
    for (const stage of ["config_format", "key_file", "region", "authentication"] as const) {
      expect(t(`settings.oci.configTest.stage.${stage}`)).not.toContain("settings.oci");
    }
    for (const status of ["success", "failed", "skipped"] as const) {
      expect(t(`settings.oci.configTest.stageStatus.${status}`)).not.toContain("settings.oci");
    }
  });
});
