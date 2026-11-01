// The cards the map opens on a mark or an area (storm points, stations,
// fires, air quality, warnings) are MapLibre popups with the sheet's surface
// put on (style.css, `.tc-popup` / `.station-popup`). Each carries a close
// button: Escape and a click elsewhere also close a card, but a phone has no
// Escape, and a click inside a warning area opens that area's card again.

import { Popup, type PopupOptions } from "maplibre-gl";
import { t } from "./i18n";

export function cardPopup(options: Omit<PopupOptions, "closeButton" | "closeOnClick">): Popup {
  const popup = new Popup({ ...options, closeButton: true, closeOnClick: false });
  // MapLibre labels its button in English; the label is read when the card
  // opens, so a card opened after a language switch speaks the new one.
  popup.on("open", () => {
    popup.getElement()?.querySelector(".maplibregl-popup-close-button")?.setAttribute("aria-label", t("cardCloseAria"));
  });
  return popup;
}
