import { test, expect } from '@playwright/test';

test('shows the readiness banner', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#delayed-banner')).toHaveText('Ready', { timeout: 1500 });
});
