// Every test file starts in metric units: jsdom reports en-US, which the
// `auto` preset reads as US customary units. Set once for whatever a file
// computes while it is collected, and again before each test in case one
// before it changed them.
import { beforeEach } from "vitest";

import { setUnitPreset } from "../../web/src/units";

setUnitPreset("metric");
beforeEach(() => {
  setUnitPreset("metric");
});
