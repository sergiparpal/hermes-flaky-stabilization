import { test, expect } from '@playwright/test';

test('renders the storefront header', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Toy Shop');
});
