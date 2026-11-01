// The warning boundary sets are PMTiles archives (docs/alerts.md §9). MapLibre
// reads them through the `pmtiles` package's protocol: a vector source names
// an archive as `pmtiles://<url>`, and the protocol answers its TileJSON and
// its tiles with range reads of that one object.

import { addProtocol } from "maplibre-gl";
import { Protocol } from "pmtiles";

export const PMTILES_PROTOCOL = "pmtiles";

let registered = false;

/** Register the protocol once; every boundary set goes through it. */
export function registerPmtilesProtocol(): void {
  if (registered) return;
  registered = true;
  addProtocol(PMTILES_PROTOCOL, new Protocol().tile);
}

/** The source URL a vector source takes for an archive. */
export function pmtilesSourceUrl(archiveUrl: string): string {
  return `${PMTILES_PROTOCOL}://${archiveUrl}`;
}
