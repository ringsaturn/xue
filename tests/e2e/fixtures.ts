/**
 * What every spec reads off disk and stubs off the network: the fixtures
 * Playwright's global setup generates (`tests/prepare_web_fixture.py`), and
 * the basemap hosts no test may depend on.
 */

import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** `tests/fixtures/generated/web/`, where the global setup writes the runs. */
const GENERATED = new URL("../fixtures/generated/web/", import.meta.url);

/** The path of a fixture, relative to `root` (the generated web fixtures by
 * default). A directory keeps its trailing slash. */
export function fixturePath(relative: string, root: URL = GENERATED): string {
  return fileURLToPath(new URL(relative, root));
}

/** A fixture's bytes. */
export function fixtureBytes(relative: string, root: URL = GENERATED): Buffer {
  return readFileSync(fixturePath(relative, root));
}

/** A JSON fixture, parsed. */
export function fixtureJson(relative: string, root: URL = GENERATED): any {
  return JSON.parse(readFileSync(fixturePath(relative, root), "utf8"));
}

/** Answer the basemap's tile requests empty. The Protomaps API key is
 * origin-locked to the production domains, so from 127.0.0.1 every tile
 * request dies on CORS — and a map whose tiles never settle occasionally
 * never fires "load", which is what gates initialize(). `relief` stubs the
 * DEM host the same way: it answers publicly, but no test should depend on
 * its bytes, and a missing DEM only means the hillshade paints nothing. */
export async function stubBasemap(page: Page, { relief = false }: { relief?: boolean } = {}): Promise<void> {
  await page.route("**/api.protomaps.com/**", (route) => route.fulfill({ status: 204, body: "" }));
  if (relief) await page.route("**/tiles.mapterhorn.com/**", (route) => route.fulfill({ status: 204, body: "" }));
}
