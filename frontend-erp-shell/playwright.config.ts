import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/smoke',
  timeout: 30_000,
  expect: {
    timeout: 10_000,
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
