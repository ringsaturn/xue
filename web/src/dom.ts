/** One element with its class and, optionally, its text: the cards and the
 * sounding section build their markup out of these. The class is always
 * set, so `""` writes an empty `class` attribute. */
export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
