import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const queriesSource = readFileSync(new URL("../src/lib/queries.ts", import.meta.url), "utf8");
const databaseSettingsSource = readFileSync(
  new URL("../src/components/settings/SelectAiCredentialCard.tsx", import.meta.url),
  "utf8",
);
const profilePageSource = readFileSync(
  new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
  "utf8",
);
const profileProgressSource = readFileSync(
  new URL("../src/features/nl2sql/components/ProfileSaveProgress.tsx", import.meta.url),
  "utf8",
);

test("Select AI Credential API keeps the fixed name and supported region contract", () => {
  assert.match(
    apiSource,
    /export type SelectAiCredentialRegion = "ap-osaka-1" \| "us-chicago-1"/u,
  );
  assert.match(apiSource, /credential_name: "NL2SQL_OCI_CRED"/u);
  assert.match(apiSource, /getSelectAiCredential/u);
  assert.match(apiSource, /createSelectAiCredential/u);
  assert.match(queriesSource, /selectAiCredential/u);
  assert.match(queriesSource, /useCreateSelectAiCredential/u);
});

test("database settings exposes create, explicit recreate, readiness and fixed feedback", () => {
  assert.match(databaseSettingsSource, /data-testid="select-ai-credential-card"/u);
  assert.match(databaseSettingsSource, /SELECT_AI_CREDENTIAL_CONFIRMATION = "ADMIN_EXECUTE"/u);
  assert.match(
    databaseSettingsSource,
    /useState<SelectAiCredentialRegion>\("us-chicago-1"\)/u,
  );
  assert.match(databaseSettingsSource, /data\.oci_auth_ready/u);
  assert.match(databaseSettingsSource, /data\.exists \? "danger" : "primary"/u);
  assert.doesNotMatch(databaseSettingsSource, /useConfirm/u);
  assert.doesNotMatch(databaseSettingsSource, /successNextStep/u);
  assert.doesNotMatch(databaseSettingsSource, /action\.openProfiles/u);
  assert.match(databaseSettingsSource, /<ErrorState[\s\S]*retryLabel/u);
  assert.match(databaseSettingsSource, /<Banner[\s\S]*severity="danger"/u);
  assert.match(databaseSettingsSource, /<ExecutionConfirmationField/u);
  assert.match(databaseSettingsSource, /<FormStatus[\s\S]*tone="danger"/u);
  assert.match(databaseSettingsSource, /data-testid="select-ai-credential-success"/u);
});

test("new Profiles use the Select AI default while existing explicit regions remain mapped", () => {
  assert.doesNotMatch(profilePageSource, /useSelectAiCredential\(\)/u);
  assert.match(profilePageSource, /const SELECT_AI_DEFAULT_REGION = "us-chicago-1"/u);
  assert.match(
    profilePageSource,
    /function emptyProfileForm\(\)/u,
  );
  assert.match(profilePageSource, /selectedProfile\s*\? profileToForm\(selectedProfile\)/u);
});

test("credential-missing Profile sync uses one fixed error surface and recovery link", () => {
  assert.match(profileProgressSource, /SELECT_AI_CREDENTIAL_MISSING/u);
  assert.match(profileProgressSource, /APP_ROUTES\.settingsSelectAiCredential/u);
  assert.match(profileProgressSource, /selectAiCredential\.precondition\.open/u);
  assert.doesNotMatch(
    profilePageSource,
    /job\.status === "failed"[\s\S]{0,160}toast\.error\(t\("profiles\.oracle\.sync\.failed"\)\)/u,
  );
  assert.doesNotMatch(
    profilePageSource,
    /toast\.error\(t\("profiles\.oracle\.sync\.savedButFailed"\)\)/u,
  );
});
