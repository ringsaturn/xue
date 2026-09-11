/** Where the site and its data live, for everything that has to name an
 * absolute URL: the canonical and hreflang links, the social card, the
 * sitemap and the discovery files a crawler or an agent reads. Relative
 * paths elsewhere stay relative — a run directory can be served from either
 * origin — so these are the only places the production hostnames appear in
 * the frontend. */

export const SITE_ORIGIN = "https://xue.ringsaturn.me";
export const SITE_NAME = "Xue";
export const REPO_URL = "https://github.com/ringsaturn/xue";
/** The public R2 bucket the deploy build reads runs from (web/.env.deploy);
 * cited by the discovery files so an agent can fetch the same bytes. */
export const DATA_ORIGIN = "https://dataset.ringsaturn.me/xue/";
