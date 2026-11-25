/**
 * Which surface an Escape closes: the one opened last, and no other.
 *
 * The sheets, the cards, the probe panel, the section panel and the map's
 * tools can all be open at once, and none of them knows of the others; each
 * puts itself on the one stack as it opens, so that one press undoes one
 * thing, in the order they were done.
 *
 * A surface says for itself whether it is open: most close by other ways
 * than this one (a button, a click elsewhere, a switch of dataset), and one
 * that has stepped aside for another keeps its place until it is back.
 */

export interface EscapeSurface {
  isOpen(): boolean;
  /** What an Escape does to it, which may leave it open (a half-drawn shape
   * ended, the tool still out). */
  close(): void;
}

export class EscapeStack {
  private surfaces: EscapeSurface[] = [];

  /** A surface has opened, or is asked for again: it is the newest now. */
  opened(surface: EscapeSurface): void {
    this.forget(surface);
    this.surfaces.push(surface);
  }

  /** A surface made for one opening (a card) is gone for good. */
  forget(surface: EscapeSurface): void {
    this.surfaces = this.surfaces.filter((held) => held !== surface);
  }

  /** Close the newest open surface; whether there was one. */
  press(): boolean {
    for (let index = this.surfaces.length - 1; index >= 0; index -= 1) {
      const surface = this.surfaces[index]!;
      if (!surface.isOpen()) continue;
      surface.close();
      return true;
    }
    return false;
  }
}

/** The page's stack. */
export const escapes = new EscapeStack();

if (typeof window !== "undefined") {
  window.addEventListener("keydown", (event) => {
    // Not one that something else has answered, nor one that ends an input
    // method's composition or folds a list up: those are the key's own.
    if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
    if (event.target instanceof HTMLSelectElement) return;
    escapes.press();
  });
}
