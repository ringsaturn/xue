/** The language tag of a product's multilingual text that suits the
 * reader: the exact tag, else one with the same primary language (`zh`
 * for `zh-Hant`, `fr` for `fr-CA`), else none. Case-insensitive, since
 * BCP 47 tags are. */
export function pickLanguageTag(available: readonly string[], wanted: string): string | null {
  const lower = wanted.toLowerCase();
  const exact = available.find((tag) => tag.toLowerCase() === lower);
  if (exact !== undefined) return exact;
  const primary = lower.split("-")[0]!;
  return available.find((tag) => tag.toLowerCase().split("-")[0] === primary) ?? null;
}
