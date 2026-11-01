/** Reading the air-quality product off the data root: the pointer → index
 * read, every network's stations table beside it, and the one range
 * request that reads a station's window. Nothing here touches the map. */

import { fetchImmutable } from "../fetchimmutable";
import { loadPointerIndex, type LoadedIndex } from "../pointerindex";
import { fetchSpanBytes, stationFileUrl } from "../stations/fetch";
import {
  AQ_POINTER_FILENAME,
  assembleAqIndex,
  parseAqIndex,
  parseAqPointer,
  parseAqStation,
  parseAqStationTable,
  type AqIndex,
  type AqPointer,
  type AqStation,
  type AqStationSeries,
} from "./schema";

export type LoadedAqIndex = LoadedIndex<AqPointer, AqIndex>;

/** The live index with every network's rows. A network whose table fails
 * to load or validate is dropped rather than taking the others with it. */
export async function fetchAqIndex(baseUrl: string): Promise<LoadedAqIndex | null> {
  const loaded = await loadPointerIndex(baseUrl, AQ_POINTER_FILENAME, parseAqPointer, parseAqIndex, "aq");
  if (loaded === null) return null;
  const { index, indexUrl } = loaded;
  const tables = await Promise.all(
    index.networks.map(async (network, position) => {
      try {
        const response = await fetchImmutable(stationFileUrl(indexUrl, network.stations));
        if (!response.ok) throw new Error(`request failed: ${response.status}`);
        return parseAqStationTable(await response.json(), index, position);
      } catch (error) {
        console.warn(`aq: dropping network ${network.id}:`, error);
        return null;
      }
    }),
  );
  return { ...loaded, index: assembleAqIndex(index, tables) };
}

/** One station's 24-hour window, read by range out of its network's file. */
export async function fetchAqStation(loaded: LoadedAqIndex, station: AqStation): Promise<AqStationSeries> {
  const network = loaded.index.networks[station.network]!;
  const url = stationFileUrl(loaded.indexUrl, network.file);
  const bytes = await fetchSpanBytes(url, station.offset, station.length);
  const parsed = parseAqStation(JSON.parse(new TextDecoder().decode(bytes)) as unknown);
  if (parsed.id !== station.id)
    throw new Error(`${network.file.path} span names ${parsed.id}, not ${station.id}`);
  return parsed;
}
