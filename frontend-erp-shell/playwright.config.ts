import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/smoke',
  // Keep one reviewed Chromium baseline across local host platforms.  The
  // Linux images are the canonical snapshots; do not create per-platform
  // copies when a Windows smoke run is used for semantic/UI checks.
  snapshotPathTemplate: '{snapshotDir}/{testFileName}-snapshots/{arg}-chromium-linux{ext}',
  timeout: 30_000,
  expect: {
    timeout: 10_000,
    // Windows and Linux Chromium render the same canonical UI with small
    // font/rasterization differences. Reuse the reviewed Linux baseline and
    // bound that host-only noise instead of creating per-platform PNG copies.
    toHaveScreenshot: {
      maxDiffPixelRatio: process.platform === 'win32' ? 0.08 : 0,
    },
  },
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:9300',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:9300',
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
