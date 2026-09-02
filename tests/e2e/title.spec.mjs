// Mix title. Both halves of P4-05 now hold: the title is editable inline AND
// persists across a reload. The second half used to be a documented gap
// (design-document.md §11: no mix entity existed in v1) and the test asserted
// the gap so it would fail loudly when persistence shipped. It shipped —
// mixes are saved server-side — so the assertion is inverted to the
// requirement the testing document actually states.
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

  test("P4-05b title persists across a reload", async ({ page }) => {
    await bootApp(page);

    const title = page.locator("#mix-title");
    await title.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("Friday Night Set");
    // Blur commits the rename to the server.
    await page.keyboard.press("Enter");
    await expect(title).toHaveText("Friday Night Set");

    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });

    // Boot resumes the most recently edited mix, so the name comes back.
    await expect(page.locator("#mix-title")).toHaveText("Friday Night Set");
    await expect(page.locator("#mix-select")).toContainText("Friday Night Set");
  });
});
