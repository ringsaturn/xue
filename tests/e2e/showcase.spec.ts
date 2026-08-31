import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The Protomaps API key is origin-locked to the production domains, so from
// 127.0.0.1 every tile request dies on CORS — and a map whose tiles never
// settle occasionally never fires "load", which is what gates initialize().
test.beforeEach(async ({ page }) => {
  await page.route("**/api.protomaps.com/**", (route) => route.fulfill({ status: 204, body: "" }));
});

const CATALOG_FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL("../fixtures/generated/web/showcase.json", import.meta.url)), "utf8"),
);
const CASE_MANIFEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/showcase/demo-typhoon/manifest.json", import.meta.url)),
    "utf8",
  ),
);
/** The case ships two of the four bundles, so the viewer has something to
 * hide and the case's own default layer differs from the app's. */
const CASE_ARTIFACTS: Record<string, Buffer> = Object.fromEntries(
  ["tmp2m.xue", "prate.xue", "tmp2m.poster.bin", "prate.poster.bin"].map((name) => [
    name,
    readFileSync(fileURLToPath(new URL(`../fixtures/generated/web/showcase/demo-typhoon/${name}`, import.meta.url))),
  ]),
);

function artifact(url: string): Buffer | undefined {
  return CASE_ARTIFACTS[new URL(url).pathname.split("/").pop() ?? ""];
}

/** The showcase's own two layers: the mutable catalog and the immutable
 * per-case manifest and artifacts it names. */
async function routeShowcase(page: Page, catalog: unknown = CATALOG_FIXTURE): Promise<void> {
  await page.route("**/data/showcase.json*", (route) => route.fulfill({ json: catalog as object }));
  await page.route("**/data/showcase/*/manifest.json*", (route) =>
    route.fulfill({ json: CASE_MANIFEST_FIXTURE }),
  );
  for (const pattern of ["**/data/showcase/*/*.poster.bin*", "**/data/showcase/*/*.xue*"]) {
    await page.route(pattern, (route) => {
      const body = artifact(route.request().url());
      if (!body) return route.fulfill({ status: 404, body: "missing" });
      return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
    });
  }
}

test("the showcase list renders a card per case and links into the viewer", async ({ page }) => {
  await routeShowcase(page);
  await page.goto("/showcase.html");
  const card = page.locator(".showcase-card").first();
  await expect(card).toBeVisible();
  await expect(card.getByRole("heading", { name: "Demo typhoon case" })).toBeVisible();
  await expect(card).toContainText("2024-09-03 00Z");
  await expect(card).toContainText("24 h");
  await expect(card).toContainText("40×30");
  await expect(card.locator("a")).toHaveAttribute("href", "./?case=demo-typhoon");
  // The card's thumbnail is the case's own first frame, decoded from the
  // poster with no WASM and no worker.
  await expect(card.locator("canvas.is-painted")).toBeVisible();
  await expect(page.locator("#showcase-status")).toBeHidden();
});

test("an empty catalog says so instead of rendering nothing", async ({ page }) => {
  await routeShowcase(page, { schemaVersion: 1, generatedAt: "2026-08-30T00:00:00Z", cases: [] });
  await page.goto("/showcase.html");
  await expect(page.locator("#showcase-status")).toHaveText("No cases published yet");
  await expect(page.locator(".showcase-card")).toHaveCount(0);
});

test("a broken catalog reports the failure", async ({ page }) => {
  await page.route("**/data/showcase.json*", (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.goto("/showcase.html");
  await expect(page.locator("#showcase-status")).toContainText("404");
});

test("?case=<id> plays the case back in the ordinary viewer", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeShowcase(page);
  await page.goto("/?case=demo-typhoon");
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 20_000 });
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toBeEnabled({ timeout: 20_000 });

  // The summary line names the case even on phones, where the panel below it
  // starts collapsed.
  await expect(page.locator("#case-chip")).toHaveText("Demo typhoon case");
  await page.locator("details.station-panel").evaluate((panel: HTMLDetailsElement) => {
    panel.open = true;
  });
  // The banner names the event and offers the way back to the list.
  await expect(page.locator("#case-title")).toHaveText("Demo typhoon case");
  await expect(page.locator("#case-region")).toContainText("110°E");
  await expect(page.getByRole("link", { name: "← All cases" })).toHaveAttribute("href", "./showcase.html");

  // A case is one fixed dataset and run: no model switch, and the run line
  // shows the historical cycle rather than a live one.
  await expect(page.locator(".model-switch")).toBeHidden();
  await expect(page.locator("body")).toHaveClass(/is-showcase/);
  await expect(page.locator("#run-time")).toContainText("09/03");
  // Only the case's own variables are offered, and it opens on the layer its
  // event is about rather than on the app's usual default.
  await expect(page.getByRole("button", { name: "TEMP 2M" })).toBeVisible();
  await expect(page.getByRole("button", { name: "PRECIP RATE" })).toBeVisible();
  await expect(page.getByRole("button", { name: "WIND 10M" })).toBeHidden();
  await expect(page.getByRole("button", { name: "SOLAR FLUX" })).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  // 25 hourly frames, f000 through f024.
  await expect(slider).toHaveAttribute("max", "24");
  await expect(page.locator("#forecast-hour")).toHaveText("F000");
});

test("a short case axis opens at a slower frame rate", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeShowcase(page);
  await page.goto("/?case=demo-typhoon");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  // 25 frames flash past in two seconds at 12 fps, so the case opens a rung
  // down — a four-second loop.
  await expect(page.getByRole("button", { name: "Playback speed" })).toHaveText("6 FPS");
});

test("an explicit ?type= overrides the case's own default layer", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeShowcase(page);
  await page.goto("/?case=demo-typhoon&type=precip");
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 20_000 });
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  // Switching layers keeps the case in the address bar and drops the model.
  // (The panel holding the switch starts collapsed on phones.)
  await page.locator("details.station-panel").evaluate((panel: HTMLDetailsElement) => {
    panel.open = true;
  });
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page).toHaveURL(/case=demo-typhoon/);
  await expect(page).toHaveURL(/type=temp/);
  await expect(page).not.toHaveURL(/model=/);
});

test("an unknown case id fails recoverably", async ({ page }) => {
  await routeShowcase(page);
  await page.goto("/?case=not-a-case");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("not-a-case");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("the live viewer links to the showcase and never reads the catalog", async ({ page }) => {
  let catalogRequests = 0;
  await page.route("**/data/showcase.json*", (route) => {
    catalogRequests += 1;
    return route.fulfill({ json: CATALOG_FIXTURE });
  });
  await page.route("**/data/latest.json*", (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("link", { name: "CASES" })).toHaveAttribute("href", "./showcase.html");
  expect(catalogRequests).toBe(0);
});
