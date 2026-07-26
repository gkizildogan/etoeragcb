import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email address').fill('admin@example.com');
  await page.getByLabel('Password').fill('Correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/\/chat/);
});

test('administrator can stream chat, save feedback, and preview citation text', async ({
  page
}) => {
  const requests: string[] = [];
  page.on('request', (request) => requests.push(request.url()));

  await expect(page.getByRole('heading', { name: 'Product handbook' })).toBeVisible();
  await page.getByLabel('Message').fill('Summarize the approval rule');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('The authoritative mock answer [S1].')).toBeVisible();

  await page.getByRole('button', { name: 'Preview' }).first().click();
  await expect(
    page.getByText('Travel requires approval from the employee’s manager before booking.')
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  expect(requests.some((url) => url.includes('/api/files/'))).toBe(false);

  await page.getByText('Rate this answer').first().click();
  await page.getByRole('button', { name: 'Helpful' }).first().click();
  await expect(page.getByText('Feedback saved').first()).toBeVisible();
});

test('administrator can upload and create a collection', async ({ page }) => {
  await page.getByRole('link', { name: 'Documents' }).click();
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible();
  await page.getByRole('button', { name: 'Upload document' }).click();
  await page.getByPlaceholder('Document title').fill('Travel addendum');
  await page.locator('input[type="file"]').setInputFiles({
    name: 'travel.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('Travel policy')
  });
  await page.getByRole('button', { name: 'Start upload' }).click();
  await expect(page.getByText('Upload accepted. Ingestion has started.')).toBeVisible();

  await page.getByRole('link', { name: 'Collections' }).click();
  await page.getByRole('button', { name: 'New collection' }).click();
  await page.getByPlaceholder('e.g. Product handbook').fill('Travel policies');
  await page.getByRole('button', { name: 'Create collection' }).click();
  await expect(page.getByText('Travel policies')).toBeVisible();
});

test('mobile navigation remains keyboard and touch accessible', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const menu = page.getByRole('button', { name: 'Open navigation' });
  await expect(menu).toBeVisible();
  await menu.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await page.getByRole('link', { name: 'Documents' }).click();
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible();
});

test('member sees inventories without administrator mutation controls', async ({ page }) => {
  await page.getByRole('button', { name: 'Sign out' }).click();
  await page.getByLabel('Email address').fill('member@example.com');
  await page.getByLabel('Password').fill('Correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in' }).click();

  await page.getByRole('link', { name: 'Documents' }).click();
  await expect(page.getByText(/An administrator manages files/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Upload document' })).toHaveCount(0);

  await page.getByRole('link', { name: 'Collections' }).click();
  await expect(page.getByText(/An administrator manages their contents/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'New collection' })).toHaveCount(0);
});

test('password-confirmed tenant switching replaces tenant-scoped session state', async ({
  page
}) => {
  const account = page.locator('details').filter({ hasText: 'Acme Knowledge' });
  await account.locator('summary').click();
  await account.getByLabel('Organization').selectOption('cccccccc-cccc-4ccc-8ccc-cccccccccccc');
  await account.getByPlaceholder('Confirm password').fill('Correct horse battery staple');
  await account.getByRole('button', { name: 'Switch organization' }).click();
  await expect(page.getByText('Other Knowledge').first()).toBeVisible();
  await expect(page).toHaveURL(/\/chat/);
});
