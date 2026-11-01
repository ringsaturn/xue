/** Reading the warnings product off the data root: the pointer → index
 * read (every poll), and a network's whole file (only while the warnings
 * are drawn — a network file is tens to hundreds of kilobytes, and the
 * map needs every line's geometry or area codes). Nothing here touches
 * the map. */

import { fetchImmutable } from "../fetchimmutable";
import { loadPointerIndex, type LoadedIndex } from "../pointerindex";
import { stationFileUrl } from "../stations/fetch";
import {
  ALERTS_POINTER_FILENAME,
  parseAlertLines,
  parseAlertsIndex,
  parseAlertsPointer,
  type AlertLine,
  type AlertsAreas,
  type AlertsIndex,
  type AlertsNetwork,
  type AlertsPointer,
} from "./schema";

export type LoadedAlertsIndex = LoadedIndex<AlertsPointer, AlertsIndex>;

export function fetchAlertsIndex(baseUrl: string): Promise<LoadedAlertsIndex | null> {
  return loadPointerIndex(
    baseUrl,
    ALERTS_POINTER_FILENAME,
    parseAlertsPointer,
    parseAlertsIndex,
    "alerts",
  );
}

/** One network's warnings in force, the whole file in one GET. */
export async function fetchAlertLines(loaded: LoadedAlertsIndex, network: AlertsNetwork): Promise<AlertLine[]> {
  const response = await fetchImmutable(stationFileUrl(loaded.indexUrl, network.file));
  if (!response.ok) throw new Error(`${network.file.path} request failed: ${response.status}`);
  return parseAlertLines(await response.text(), network.id);
}

/** The absolute, `?v=`-addressed URL of a network's boundary set. */
export function alertsAreasUrl(loaded: LoadedAlertsIndex, areas: AlertsAreas): string {
  return stationFileUrl(loaded.indexUrl, areas);
}
