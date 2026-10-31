import { defineConfig, devices } from "@playwright/test";

const storageState = process.env.OCI_RESOURCE_MANAGER_STORAGE_STATE;

export default defineConfig({
  testDir: "./tests/oci-resource-manager",
  timeout: 60_000,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: false,
  reporter: process.env.CI ? "dot" : "list",
  use: {
    storageState: storageState || undefined,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "mobile-375",
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 375, height: 812 },
        isMobile: true,
      },
    },
  ],
});
