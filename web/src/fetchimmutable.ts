/** GET one immutable, `?v=`-addressed artifact, working around a poisoned
 * browser cache.
 *
 * Every run artifact is served with `Vary: Origin` and a year-long immutable
 * lifetime, and the bucket sends `access-control-allow-origin` only on a
 * cross-origin request. Safari keys its disk cache by URL alone, so once a
 * response without that header is in it — the URL opened directly in a tab,
 * where a navigation carries no Origin — every later cross-origin fetch of
 * the same URL is answered from that entry and rejected by CORS, for a year.
 * Chrome honors `Vary` and never gets there.
 *
 * The rejection surfaces as fetch throwing (a `TypeError`: "Load failed" in
 * Safari, "Failed to fetch" in Chrome), indistinguishable from the network
 * being down, so any throw earns one retry with the cache skipped. `reload`
 * rather than `no-store`: both bypass the entry, but `reload` also replaces
 * it with the response it got, so the next visit does not pay the detour
 * again. A genuine outage costs one extra failed request. */
export async function fetchImmutable(url: string | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    return fetch(url, { ...init, cache: "reload" });
  }
}
