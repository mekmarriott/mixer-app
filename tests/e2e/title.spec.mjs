// Mix title. P4-05 has two halves and the second is a documented gap, not a
// bug — design-document.md §11 records that no mix-save entity exists in v1.
// The test asserts both the working half and the current known behaviour, so
// shipping persistence will make the second expectation fail loudly and
// prompt this test (and the manifest row) to be updated.
import { test, expect } from "@playwright/test";
import { bootApp } from "./helpers.mjs";

test.describe("mix title", () => {
  test("P4-05a title is editable inline", async ({ page }) => {
    await bootApp(page);

    const title = page.locator("#mix-title");
    await expect(title).toHaveText("Untitled Mix");
    await expect(title).toHaveAttribute("contenteditable", "true");

    await title.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("Friday Night Set");
    await expect(title).toHaveText("Friday Night Set");

    // Enter commits and blurs rather than inserting a newline.
    await page.keyboard.press("Enter");
    await expect(title).toHaveText("Friday Night Set");
    expect(await page.evaluate(() => document.activeElement?.id)).not.toBe("mix-title");
  });

  test("P4-05b title does NOT persist across reload (known gap, design-document §11)", async ({ page }) => {
    await bootApp(page);

    const title = page.locator("#mix-title");
    await title.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("Friday Night Set");
    await expect(title).toHaveText("Friday Night Set");

    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible();

    // Documents the deliberate v1 behaviour: there is no mix-save feature, so
    // the title resets. Flip this to toHaveText("Friday Night Set") when a
    // mix entity ships.
    await expect(page.locator("#mix-title")).toHaveText("Untitled Mix");
  });
});
