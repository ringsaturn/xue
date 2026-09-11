/** The one sheet mechanism the shell has: a trigger, a scrim and a panel that
 * sits under the trigger on desktop and docks to the bottom edge on phones.
 * The model picker, the sources list and the language picker are all this,
 * and the viewer and the showcase page share it rather than each growing
 * their own.
 *
 * What a sheet owes its viewer: the trigger says whether it is open
 * (`aria-expanded`), a click on anything marked `data-sheet-dismiss` — the
 * scrim, a close button — shuts it, Escape shuts it, and focus moves into the
 * panel on open and comes back to the trigger on close. */

export interface SheetOptions {
  /** The control that opens it; carries `aria-expanded`. */
  trigger: HTMLElement;
  /** The `.model-sheet` wrapper: scrim plus panel, `hidden` while closed. */
  sheet: HTMLElement;
  /** Whether opening is allowed at this moment. A sheet that cannot open is
   * simply not opened; the trigger stays `aria-expanded="false"`. */
  canOpen?: () => boolean;
  /** What to focus when it opens. Defaults to the first focusable row. */
  initialFocus?: (sheet: HTMLElement) => HTMLElement | null | undefined;
}

export interface SheetController {
  open(): void;
  close(): void;
  toggle(): void;
  isOpen(): boolean;
}

const FOCUSABLE = 'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/** Wire one sheet and hand back its controls. Every listener it needs — the
 * trigger, the dismiss targets, Escape — is attached here. */
export function createSheet({ trigger, sheet, canOpen, initialFocus }: SheetOptions): SheetController {
  const isOpen = () => !sheet.hidden;

  function setOpen(open: boolean): void {
    const allowed = open && (canOpen?.() ?? true);
    // Focus must leave the panel before it is hidden: a focused element
    // inside a `hidden` subtree leaves the page with nothing focused.
    if (!allowed && document.activeElement instanceof HTMLElement && sheet.contains(document.activeElement)) {
      trigger.focus();
    }
    sheet.hidden = !allowed;
    trigger.setAttribute("aria-expanded", String(allowed));
    if (!allowed) return;
    const target = initialFocus?.(sheet) ?? sheet.querySelector<HTMLElement>(FOCUSABLE);
    target?.focus();
  }

  trigger.addEventListener("click", () => setOpen(!isOpen()));
  sheet.addEventListener("click", (event) => {
    if ((event.target as HTMLElement).closest("[data-sheet-dismiss]")) setOpen(false);
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) setOpen(false);
  });

  return {
    open: () => setOpen(true),
    close: () => setOpen(false),
    toggle: () => setOpen(!isOpen()),
    isOpen,
  };
}

/** Build the language picker's rows into its panel and wire the choice.
 * Both pages carry the same markup — a `.model-sheet` wrapper whose panel
 * holds an empty `.lang-list` — so both build it the same way. The endonyms
 * are never translated: each row names its own language, in its own script,
 * and carries `lang` so a screen reader reads it in that language. */
export function fillLanguageList<Code extends string>(
  list: HTMLElement,
  locales: readonly { code: Code; endonym: string }[],
  options: { current: Code; htmlLang: (code: Code) => string; onPick: (code: Code) => void },
): void {
  list.replaceChildren(
    ...locales.map(({ code, endonym }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.locale = code;
      button.lang = options.htmlLang(code);
      // The active row is marked the way the model sheet marks the active
      // model — a check on the right — and `aria-current` says so out loud.
      if (code === options.current) button.setAttribute("aria-current", "true");
      const name = document.createElement("span");
      name.className = "lang-name";
      name.textContent = endonym;
      const check = document.createElement("span");
      check.className = "model-check";
      check.setAttribute("aria-hidden", "true");
      button.append(name, check);
      button.addEventListener("click", () => options.onPick(code));
      return button;
    }),
  );
}
