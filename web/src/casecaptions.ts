/** A showcase case's captions (`showcase-catalog.ts` `ShowcaseCaption`): one
 * line of text over the map while the playhead is inside its span, the way a
 * film carries subtitles. The spans are in valid time, not in frames, so the
 * same caption holds at any playback speed and in an export. */

import { localizedText, type ShowcaseCaption } from "./showcase-catalog";

/** The caption the playhead is inside, if any. The spans run forward and
 * never overlap (the catalog drops one that would), so a binary search
 * finds the only candidate. */
export function captionAt(captions: readonly ShowcaseCaption[], time: number): ShowcaseCaption | null {
  let low = 0;
  let high = captions.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const caption = captions[middle]!;
    if (time < caption.from) high = middle - 1;
    else if (time >= caption.to) low = middle + 1;
    else return caption;
  }
  return null;
}

/** The caption's line in `locale`, or "" when the playhead is in none. */
export function captionText(captions: readonly ShowcaseCaption[], time: number, locale: string): string {
  const caption = captionAt(captions, time);
  return caption ? localizedText(caption.text, locale) : "";
}
