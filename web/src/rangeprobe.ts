/** What each object's origin has answered about ranges, by URL. */
const rangeProbes = new Map<string, Promise<boolean>>();

/** How long a probe may go unanswered. */
const RANGE_PROBE_MS = 10_000;

/**
 * True when the server honors single byte ranges with exact 206 responses,
 * which is what the on-demand streaming paths require. A single-range GET is
 * a CORS-safelisted request, so this needs no preflight.
 *
 * The answer is the origin's about one object, so it is asked once: a shared
 * store is probed by every session that opens a bundle out of it, and a pin
 * with a model column opens some forty. The one byte of a 206 is read
 * through rather than cancelled, because a cancelled response is a reset
 * HTTP/2 stream, and an edge that sees a run of those from one client takes
 * it for an attack and closes the connection — with every frame and series
 * then in flight on it. Only an origin that ignores the range and starts
 * sending the whole object is cut off, since that body may be a whole
 * bundle. A probe that fails outright, or goes unanswered for
 * `RANGE_PROBE_MS`, is not remembered: the next session asks again.
 */
export function supportsRangeRequests(url: string): Promise<boolean> {
  const known = rangeProbes.get(url);
  if (known) return known;
  const probe = (async () => {
    // The answer is remembered, and every session of the store waits on
    // it: a probe that never came back would hold all of them for good, so
    // it is given up like one that failed, and asked again by the next.
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const quiet = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error("range probe went unanswered"));
      }, RANGE_PROBE_MS);
    });
    quiet.catch(() => {});
    try {
      const response = await Promise.race([fetch(url, { headers: { Range: "bytes=0-0" }, signal: controller.signal }), quiet]);
      if (response.status !== 206) {
        await Promise.race([response.body?.cancel(), quiet]);
        return false;
      }
      await Promise.race([response.arrayBuffer(), quiet]);
      return true;
    } catch {
      rangeProbes.delete(url);
      return false;
    } finally {
      clearTimeout(timer);
    }
  })();
  rangeProbes.set(url, probe);
  return probe;
}
