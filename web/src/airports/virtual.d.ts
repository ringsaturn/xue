/** The ICAO ids of the airports with a static page, from the checked-in
 * list (`web/tooling/airports.json`) at build time, so the shell carries the
 * ids and not the list (`airportPageIds` in `web/tooling/airportpages.ts`). */
declare module "virtual:airport-pages" {
  export const AIRPORT_PAGE_ICAOS: readonly string[];
}
