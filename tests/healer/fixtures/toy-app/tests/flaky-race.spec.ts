import { test, expect } from '@playwright/test';

test('loads the item list', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('load-items').click();
  await expect(page.locator('#item-count')).toHaveText('3 items', { timeout: 1000 });
});
