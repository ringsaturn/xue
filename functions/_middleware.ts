/** The Cloudflare Pages Function at the site root: an old viewer link that
 * names its dataset as `?model=` (`/?model=hrrr&type=wind`) is answered
 * with a 301 to the dataset's path (`/hrrr?type=wind`), the rest of the
 * query kept and the fragment left to the browser. A spelling of no live
 * dataset goes to `/` without the parameter; a case link, and everything
 * else, passes through to the static site.
 *
 * `wrangler pages deploy` builds this directory (`functions/` beside the
 * command, the repository root) into the deployment, and
 * `web/public/_routes.json` limits it to `/` and `/index.html`, so no other
 * request runs it. The mapping is the shell's own (`modelpaths.ts`), which
 * applies it again on boot where no function ran. */

import { FORECAST_MODEL_IDS, hasModelPage, legacyModelLocation } from "../web/src/modelpaths";

/** Every dataset with a page on the deployed site: the deploy build sets
 * no `VITE_SOURCES`, so it emits a page for each one with a page at all
 * (`modelpages.ts`); `?model=` naming one without passes through. */
const PATH_IDS: ReadonlySet<string> = new Set(FORECAST_MODEL_IDS.filter(hasModelPage));

interface PagesContext {
  request: Request;
  next: () => Promise<Response>;
}

export async function onRequest(context: PagesContext): Promise<Response> {
  const url = new URL(context.request.url);
  const location = legacyModelLocation(url.pathname, url.search, PATH_IDS);
  if (location === null) return context.next();
  return Response.redirect(new URL(location, url).href, 301);
}
