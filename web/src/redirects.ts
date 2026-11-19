/** Where an old link belongs: the one mapping the Pages Function at the
 * site root answers with a 301 (`functions/_middleware.ts`) and the shell
 * applies with `replaceState` on boot, in two steps — `i18n.ts` places the
 * language and the case before anything reads the address, `main.ts` the
 * dataset once the manifest's table is known.
 *
 * 1. `?lang=<tag>` and `/?case=<id>` (`sitepaths.ts::legacyLocaleLocation`):
 *    `/?case=x&lang=ja` → `/ja/showcase/x`, `/compare?lang=de` → `/de/compare`.
 * 2. `?model=<alias>` on a home (`modelpaths.ts::legacyModelLocation`):
 *    `/ja/?model=hrrr` → `/ja/hrrr`, so `/?model=hrrr&lang=ja` → `/ja/hrrr`.
 *
 * Nothing here imports at run time beyond those two modules. */

import { legacyModelLocation } from "./modelpaths";
import { legacyLocaleLocation } from "./sitepaths";

/** The root-relative path and query an old link belongs at, or null when it
 * is fine where it is. `pathIds` are the datasets with a page. */
export function legacyLocation(pathname: string, search: string, pathIds: ReadonlySet<string>): string | null {
  const placed = legacyLocaleLocation(pathname, search);
  const url = new URL(placed ?? `${pathname}${search}`, "https://localhost");
  const moved = legacyModelLocation(url.pathname, url.search, pathIds);
  return moved ?? placed;
}
