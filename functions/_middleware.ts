/** The Cloudflare Pages Function in front of the shell's pages: an old link
 * is answered with a 301 to where it belongs now (`web/src/redirects.ts`) —
 * `?lang=` moves the page under its language's prefix
 * (`/compare?lang=ja` → `/ja/compare`), `/?case=<id>` is the case's page
 * (`/showcase/<id>`), and `?model=` on a home is the dataset's path
 * (`/?model=hrrr&type=wind` → `/hrrr?type=wind`); the rest of the query is
 * kept and the fragment left to the browser. Everything else passes
 * through to the static site.
 *
 * `wrangler pages deploy` builds this directory (`functions/` beside the
 * command, the repository root) into the deployment, and
 * `web/public/_routes.json` limits it to the pages a link from before
 * could name (`/`, `/index.html` and the shell's other pages), so no other
 * request runs it. The mapping is the shell's own, which applies it again
 * on boot where no function ran.
 *
 * An agent asking for `/` with `Accept: text/markdown` gets `llms.txt`,
 * the site's own Markdown description, instead of a page that is only a
 * script tag before it runs. */

import { FORECAST_MODEL_IDS, hasModelPage } from "../web/src/modelpaths";
import { legacyLocation } from "../web/src/redirects";

/** Every dataset with a page on the deployed site: the deploy build sets
 * no `VITE_SOURCES`, so it emits a page for each one with a page at all
 * (`web/tooling/sitepages.ts`); `?model=` naming one without passes
 * through. */
const PATH_IDS: ReadonlySet<string> = new Set(FORECAST_MODEL_IDS.filter(hasModelPage));

interface PagesContext {
  request: Request;
  next: () => Promise<Response>;
  env?: { ASSETS?: { fetch: (request: Request | URL) => Promise<Response> } };
}

function wantsMarkdown(request: Request): boolean {
  return /(^|,)\s*text\/markdown\b/i.test(request.headers.get("accept") ?? "");
}

export async function onRequest(context: PagesContext): Promise<Response> {
  const url = new URL(context.request.url);
  const location = legacyLocation(url.pathname, url.search, PATH_IDS);
  if (location !== null) return Response.redirect(new URL(location, url).href, 301);
  if (url.pathname !== "/") return context.next();
  const assets = context.env?.ASSETS;
  if (assets && wantsMarkdown(context.request)) {
    const llms = await assets.fetch(new URL("/llms.txt", url));
    if (llms.ok) {
      return new Response(llms.body, {
        headers: { "content-type": "text/markdown; charset=utf-8", vary: "Accept" },
      });
    }
  }
  const page = await context.next();
  const response = new Response(page.body, page);
  response.headers.append("vary", "Accept");
  return response;
}
