import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";
import "./workbench/workbench.css";

import { applyStaticMessages } from "./i18n";
import { applyPageMeta } from "./pagemeta";
import { applyTheme } from "./theme";
import { mountWorkbench } from "./workbench/page";

/**
 * The workbench: one forecast read in depth along a line or at a point, the
 * section, the profile and the hours ahead side by side on one axis of
 * pressure. `workbench/page.ts` puts it together.
 */

applyStaticMessages();
applyTheme();
applyPageMeta({ path: "/workbench" });
mountWorkbench(document.getElementById("workbench") as HTMLElement);
