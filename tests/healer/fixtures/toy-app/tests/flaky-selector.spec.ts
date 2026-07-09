import { test, expect } from '@playwright/test';

test('submits the order form', async ({ page }) => {
  await page.goto('/');
  await page.locator('#btn-1f9c').click({ timeout: 2000 });
  await expect(page.locator('#submit-result')).toHaveText('submitted');
});
