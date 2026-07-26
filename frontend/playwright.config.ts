import { defineConfig, devices } from '@playwright/test';

const sessionSecret = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'retain-on-failure',
    video: 'retain-on-failure'
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ],
  webServer: [
    {
      command: 'node tests/mock-api.mjs',
      port: 4174,
      reuseExistingServer: !process.env.CI
    },
    {
      command: `API_INTERNAL_URL=http://127.0.0.1:4174/api FRONTEND_SESSION_SECRET=${sessionSecret} ORIGIN=http://localhost:4173 HOST=127.0.0.1 PORT=4173 node build`,
      port: 4173,
      reuseExistingServer: !process.env.CI
    }
  ]
});
