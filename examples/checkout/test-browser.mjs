import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.LOGFORGE_PLAYWRIGHT_PATH || "playwright");
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("http://127.0.0.1:38500");
  await page.getByRole("button", { name: "Place order" }).click();
  await page.locator('#status[data-ready="true"]').waitFor();
  assert.equal(await page.locator("#status").textContent(), "Order 1 saved");
  assert.deepEqual(errors, []);
} finally { await browser.close(); }
