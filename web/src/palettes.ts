import { identityForBundleId, type VariableIdentity } from "./identity";
import {
  CAPE_CHART_MAX,
  CIN_CHART_RANGE,
  DEW_POINT_CHART_RANGE,
  GUST_SPEED_MAX,
  ICE_THICKNESS_CHART_MAX,
  OMEGA_PALETTE_MAX,
  PBL_CHART_MAX,
  PTYPE_CLASSES,
  PWAT_CHART_MAX,
  VISIBILITY_CHART_MAX,
  WAVE_HEIGHT_CHART_MAX,
  WAVE_PERIOD_CHART_MAX,
  aerosolChart,
  isobaricRange,
  temperaturePaletteDomain,
  thetaEPaletteDomain,
} from "./levels";
import type { BundleVariable, LinearQuantization, LogQuantization } from "./manifest";

/**
 * Color ramps in physical units. The shader samples them per pixel, so the
 * decoded planes stay single-channel and coloring is a pure GPU concern.
 */
type Stop = [value: number, r: number, g: number, b: number, a: number];

const TEMPERATURE_STOPS: Stop[] = [
  [-60, 39, 25, 89, 255],
  [-50, 49, 54, 149, 255],
  [-40, 55, 103, 190, 255],
  [-30, 65, 155, 201, 255],
  [-20, 111, 201, 183, 255],
  [-10, 181, 226, 174, 255],
  [0, 238, 239, 179, 255],
  [10, 254, 217, 118, 255],
  [20, 253, 153, 66, 255],
  [30, 230, 85, 48, 255],
  [40, 179, 32, 55, 255],
  [50, 112, 20, 65, 255],
];

// The top of the ramp deepens through violet instead of washing out to
// near-white, and extends to the codebook's 128 mm/h ceiling so extreme
// cells keep their own gradation rather than sharing one color above 50.
// The low end (<= 2 mm/h) climbs from nearly transparent to opaque, which is
// what makes drizzle legible over the dark theme's basemap. One ramp serves
// both themes, so on the light theme's white ground those same faint blues
// read weaker than they do on a slate — a known trade, taken so a value is
// the same color in both themes rather than two palettes drifting apart.
const PRECIPITATION_STOPS: Stop[] = [
  [0, 8, 19, 28, 0],
  [0.05, 64, 131, 183, 70],
  [0.1, 82, 157, 205, 120],
  [0.5, 96, 186, 219, 175],
  [1, 90, 210, 200, 205],
  [2, 111, 219, 158, 225],
  [5, 213, 225, 101, 235],
  [10, 255, 190, 75, 245],
  [20, 245, 112, 72, 250],
  [30, 205, 61, 98, 255],
  [40, 155, 46, 133, 255],
  [50, 118, 37, 138, 255],
  [80, 84, 28, 125, 255],
  [128, 48, 14, 84, 255],
];

// Solar radiation ramp: night stays transparent so
// the terminator reads as the basemap fading through, then an inferno-like
// progression — deep violet dawn light through orange to near-white noon
// glare at the 1270 W/m² codebook ceiling. Values are W/m².
const SOLAR_STOPS: Stop[] = [
  [0, 12, 8, 34, 0],
  [30, 36, 15, 82, 80],
  [100, 84, 21, 110, 140],
  [200, 130, 37, 108, 180],
  [350, 180, 54, 92, 210],
  [500, 222, 81, 65, 230],
  [650, 246, 118, 34, 242],
  [800, 252, 163, 27, 250],
  [950, 250, 208, 58, 255],
  [1100, 252, 240, 130, 255],
  [1270, 255, 253, 210, 255],
];

// Radar composite reflectivity, in the dBZ classes a Chinese (and US) radar
// mosaic is read in: cyan and blue drizzle, green light rain, yellow through
// red for the convective core, magenta and violet for hail-sized echoes.
// The classes are interpolated rather than stepped because the shader
// reconstructs the code field bicubically and blends two frames before the
// lookup — hard class edges would shimmer as codes cross them. Alpha rises
// from nothing at 0 dBZ, which is both "no echo" and "outside radar
// coverage": a mosaic covers only the land its network watches, and the
// codebook has no separate value for the difference.
const REFLECTIVITY_STOPS: Stop[] = [
  [0, 0, 236, 236, 0],
  [5, 0, 236, 236, 70],
  [10, 1, 160, 246, 130],
  [15, 0, 80, 236, 185],
  [20, 0, 216, 96, 220],
  [25, 0, 200, 0, 235],
  [30, 0, 144, 0, 245],
  [35, 255, 255, 0, 255],
  [40, 231, 192, 0, 255],
  [45, 255, 144, 0, 255],
  [50, 255, 0, 0, 255],
  [55, 214, 0, 0, 255],
  [60, 192, 0, 0, 255],
  [65, 255, 0, 240, 255],
  [70, 150, 0, 180, 255],
  [80, 240, 233, 255, 255],
];

// The satellite infrared window, brightness temperature in the file's
// kelvin: the inverted grey scale every infrared picture is drawn in —
// warm sea and land dark, cloud brighter the colder its top — with the
// cold tops enhanced in colour the way the common convective IR products
// (and Windy's "Infra+") do it: the colour starts near -30 °C and runs
// light blue, blue, green, yellow, orange, red, purple and finally white
// at the coldest overshooting tops near -90 °C, so a reader tells a
// -50 °C anvil from a -75 °C core at a glance. Alpha rises from nothing
// at the codebook bottom, 180 K, which is also what the cells outside the
// disk carry; the warm end stays short of black and fully opaque, so the
// picture reads as a picture and the coastline drawn over it still shows.
// The stops are the shell's own, not another product's table.
const BRIGHTNESS_TEMPERATURE_STOPS: Stop[] = [
  [180, 255, 255, 255, 0],
  [181.5, 255, 255, 255, 255],
  [183.15, 250, 250, 255, 255],
  [188.15, 215, 170, 245, 255],
  [193.15, 140, 40, 200, 255],
  [198.15, 190, 20, 90, 255],
  [203.15, 225, 30, 30, 255],
  [208.15, 255, 100, 0, 255],
  [213.15, 255, 170, 0, 255],
  [218.15, 250, 235, 40, 255],
  [223.15, 70, 200, 70, 255],
  [228.15, 40, 150, 110, 255],
  [233.15, 40, 110, 230, 255],
  [238.15, 100, 180, 240, 255],
  [243.15, 190, 225, 240, 255],
  [246.15, 236, 236, 236, 255],
  [258.15, 205, 205, 205, 255],
  [273.15, 155, 155, 155, 255],
  [288.15, 100, 100, 100, 255],
  [300.15, 56, 56, 56, 255],
  [315.15, 32, 32, 32, 255],
  [332, 14, 14, 14, 255],
];

// The DEBRA dust confidence, in [0, 1]: nothing under 0.1, where the
// combined factor is noise, then the yellow the operational product paints
// dust in — a translucent pale straw at the first evidence, the saturated
// golden yellow of a confident plume, orange where the tests all agree —
// with the alpha rising to opaque, so a plume reads as a plume and clear
// sky is the map beneath. Code 0 is no data (outside the disk, an input
// the slot lacked, land with no emissivity staged) and never reaches the
// ramp: the shell erodes it (`floorIsNoData`).
const DUST_CONFIDENCE_STOPS: Stop[] = [
  [0, 255, 240, 170, 0],
  [0.1, 255, 240, 170, 0],
  [0.2, 255, 234, 150, 120],
  [0.3, 255, 226, 122, 175],
  [0.45, 255, 200, 70, 215],
  [0.6, 255, 176, 0, 240],
  [0.8, 255, 130, 0, 255],
  [1, 255, 106, 0, 255],
];

// The pressure family shares one ramp, given in fractions of the level's own
// codebook range rather than absolute values: a fill under contour lines is
// read as "low here, high there", and every level would otherwise need its
// own table of metres. Low-saturation on purpose — the lines are the
// subject, and the fill is composited beneath them at partial alpha, so a
// vivid ramp would compete with them for the eye. Cool for troughs and lows,
// warm for ridges and highs, the way a filled height chart is conventionally
// coloured.
const PRESSURE_UNIT_STOPS: Stop[] = [
  [0.0, 52, 66, 120, 255],
  [0.2, 62, 104, 148, 255],
  [0.4, 82, 142, 150, 255],
  [0.55, 128, 166, 136, 255],
  [0.7, 186, 172, 116, 255],
  [0.85, 196, 132, 100, 255],
  [1.0, 168, 82, 92, 255],
];

/** The pressure ramp mapped onto one variable's own codebook range. */
function pressureStops(quantization: LinearQuantization): Stop[] {
  const low = quantization.offset;
  const high = quantization.offset + quantization.scale * quantization.maximumCode;
  return PRESSURE_UNIT_STOPS.map(
    ([unit, r, g, b, a]) => [low + unit * (high - low), r, g, b, a] as Stop,
  );
}

/** A ramp given over one range, laid over another: the stop values move
 * linearly, the colours stay. */
function remapStops(stops: Stop[], from: readonly [number, number], to: readonly [number, number]): Stop[] {
  const scale = (to[1] - to[0]) / (from[1] - from[0]);
  return stops.map(([value, r, g, b, a]) => [to[0] + (value - from[0]) * scale, r, g, b, a] as Stop);
}

// Relative humidity, the way a moisture chart is read: dry air in the browns
// of bare ground, the middle in paper tones, saturated air in greens deepening
// to blue past 90 %, where cloud and rain live. The field covers everything —
// there is no "nothing here" — but it is drawn as a light wash rather than a
// coat: the colours are kept pale and unsaturated, and the transparency runs
// from under half where dry to three quarters at saturation, so the map's
// coastlines and names stay legible under it and the pressure lines drawn
// over it (one ink per theme) keep their contrast against a ground the
// theme's slate or paper still shows through. On the dark theme it takes
// the precipitation slate (main.ts) so a wash this light has a ground.
const HUMIDITY_STOPS: Stop[] = [
  [0, 150, 104, 58, 110],
  [20, 192, 150, 100, 110],
  [40, 222, 206, 165, 110],
  [55, 232, 232, 210, 115],
  [70, 186, 220, 186, 140],
  [80, 130, 196, 165, 160],
  [90, 88, 162, 176, 180],
  [100, 60, 116, 176, 195],
];

// Specific humidity in fractions of the level's codebook ceiling: dry air is
// transparent (the map reads through as it does under precipitation), and
// the moist end runs pale green through teal to a deep blue.
const SPECIFIC_HUMIDITY_UNIT_STOPS: Stop[] = [
  [0.0, 200, 220, 200, 0],
  [0.08, 190, 222, 170, 120],
  [0.2, 140, 204, 140, 180],
  [0.35, 90, 184, 150, 215],
  [0.5, 60, 150, 170, 235],
  [0.7, 50, 104, 176, 245],
  [1.0, 56, 48, 140, 255],
];

// Water vapour flux magnitude, g·cm⁻¹·hPa⁻¹·s⁻¹. Nothing below 2 — dry or
// calm air is the map — then greens into teal, blue and violet for the
// conveyor belts a rainstorm feeds on (20–40 is strong transport). The
// transparency climbs slowly through the background flux (5–10 is most of
// an ocean) and only firms up past 15, so the belts stand out of the map
// instead of out of a green veil.
export const VAPOUR_FLUX_STOPS: Stop[] = [
  [0, 160, 210, 170, 0],
  [2, 160, 210, 170, 0],
  [5, 130, 200, 140, 50],
  [10, 90, 186, 150, 120],
  [15, 70, 164, 176, 170],
  [20, 60, 132, 196, 210],
  [30, 72, 96, 200, 245],
  [40, 110, 70, 190, 252],
  [50, 140, 50, 170, 255],
];

// Wind speed ramp for the GPU particle layer: the
// familiar blue -> teal -> green -> yellow -> orange -> red -> violet
// progression (earth.nullschool / Windy convention). Values are m/s.
export const WIND_SPEED_MAX = 40;
const WIND_SPEED_STOPS: Stop[] = [
  [0, 110, 124, 195, 255],
  [5, 82, 157, 191, 255],
  [10, 92, 190, 140, 255],
  [15, 191, 205, 92, 255],
  [20, 235, 167, 74, 255],
  [25, 238, 112, 66, 255],
  [30, 212, 60, 87, 255],
  [35, 170, 51, 133, 255],
  [40, 129, 55, 168, 255],
];

// Total cloud cover: clear sky is the map, and cover is a grey veil that
// thickens with the fraction — the alpha carries the reading, the colour
// barely moves. One neutral grey rather than white on purpose: a white veil
// vanishes on the light theme's paper, and a light grey reads as cloud on
// the dark slate and as overcast on paper alike. Values are percent.
const CLOUD_STOPS: Stop[] = [
  [0, 170, 178, 190, 0],
  [10, 172, 180, 192, 40],
  [25, 176, 184, 196, 95],
  [50, 184, 191, 202, 160],
  [75, 192, 198, 208, 205],
  [90, 200, 206, 215, 232],
  [100, 208, 213, 221, 245],
];

// CAPE, in the classes a severe-weather chart draws: nothing under 100 J/kg
// (stable air is the map), pale straw through yellow for the marginal
// hundreds, amber and orange past 1000, red at 2000, crimson at 3000 and
// violet for the extreme — the SPC / Windy progression, so a reader who
// knows one reads the other. Saturates at 5000; the codebook runs on to
// 6350 so a probe still tells 5500 from 6000. Values are J/kg.
export const CAPE_STOPS: Stop[] = [
  [0, 250, 240, 180, 0],
  [100, 250, 240, 180, 60],
  [250, 240, 228, 120, 120],
  [500, 232, 208, 80, 170],
  [1000, 240, 170, 50, 210],
  [1500, 240, 128, 40, 230],
  [2000, 226, 80, 40, 245],
  [3000, 190, 30, 60, 252],
  [4000, 140, 30, 110, 255],
  [CAPE_CHART_MAX, 90, 30, 130, 255],
];

// Precipitable water in kg/m² — numerically a millimetre of water — as a
// rising moisture ramp: a dry column is nearly the map, the middle thickens
// through teal and blue, and a saturated tropical column deepens to violet
// at the 80 mm chart ceiling. The same reading as the humidity and dew point
// ramps, on the column's own scale. Values are kg/m².
const PWAT_STOPS: Stop[] = [
  [0, 200, 220, 200, 0],
  [5, 190, 222, 180, 70],
  [10, 170, 216, 165, 130],
  [20, 130, 205, 150, 185],
  [30, 90, 190, 160, 215],
  [45, 60, 160, 185, 240],
  [60, 50, 115, 195, 250],
  [PWAT_CHART_MAX, 60, 60, 150, 255],
];

// Convective inhibition in J/kg, one-sided the other way to CAPE: no cap at
// 0 J/kg is the map, and the ramp deepens down to a lifted parcel twice held
// back, through a cool blue that is the opposite side of the CAPE ramp's
// warm core — a violet at the -200 J/kg chart ceiling, past which the
// codebook runs on to -1016 for a probe. Values are J/kg, always <= 0.
const CIN_STOPS: Stop[] = [
  [CIN_CHART_RANGE[0], 90, 30, 130, 255],
  [-150, 70, 55, 165, 250],
  [-100, 55, 95, 190, 235],
  [-60, 70, 140, 205, 210],
  [-30, 120, 185, 215, 160],
  [-10, 180, 220, 225, 60],
  [0, 220, 225, 220, 0],
];

// Planetary boundary layer height in metres, drawn as a terrain ramp: a
// shallow night or winter layer is nearly the map, the mixing layer climbs
// through lowland green and tan, and a deep desert or marine layer runs to
// the brown of high ground at the 4 km chart ceiling. Values are metres.
const PBL_STOPS: Stop[] = [
  [0, 60, 120, 80, 0],
  [200, 90, 150, 90, 90],
  [500, 130, 175, 95, 150],
  [1000, 185, 195, 110, 195],
  [1500, 215, 195, 120, 220],
  [2000, 225, 170, 100, 240],
  [2500, 205, 130, 80, 248],
  [3000, 170, 95, 70, 252],
  [PBL_CHART_MAX, 150, 70, 60, 255],
];

// Precipitation type as a categorical palette over WMO code table 4.201's
// values: rain blue, freezing rain pink, snow a pale white-blue, ice pellets
// purple, with code 0 (no precipitation) transparent. A discrete field, so
// the stops sit exactly on the class codes; an interpolated code the encoder
// never writes (2, 4, 6, 7) blends between neighbours, which is harmless.
const PTYPE_STOPS: Stop[] = [
  [0, 0, 0, 0, 0],
  ...PTYPE_CLASSES.map(({ code, rgb }) => [code, rgb[0], rgb[1], rgb[2], 255] as Stop),
];

// Vertical velocity ω, a diverging ramp about zero: nothing within
// ±0.1 Pa/s, where most of a field sits and the small values are noise,
// ascent (negative ω, the side a rainfall chart shades) in blues deepening
// to violet, descent in a fainter amber to brown — subsidence is the quieter
// half of the story, so it gets the quieter colours. Values are Pa/s; the
// ramp saturates at ±2.5 and the codebook runs on to ±6.35.
export const OMEGA_STOPS: Stop[] = [
  [-OMEGA_PALETTE_MAX, 70, 30, 130, 255],
  [-1.5, 60, 70, 180, 250],
  [-1.0, 50, 120, 200, 240],
  [-0.5, 90, 170, 210, 200],
  [-0.25, 150, 205, 220, 120],
  [-0.1, 190, 225, 230, 0],
  [0, 220, 220, 210, 0],
  [0.1, 235, 215, 170, 0],
  [0.25, 232, 195, 130, 110],
  [0.5, 222, 165, 90, 180],
  [1.0, 200, 125, 60, 225],
  [1.5, 170, 90, 45, 240],
  [OMEGA_PALETTE_MAX, 130, 60, 35, 250],
];

// Equivalent potential temperature in fractions of the level's hundred-kelvin
// window: cold dry air in deep blue, through teal and green, to the yellow,
// orange and red of a warm moist tropical air mass — the way a Chinese
// 850 hPa θse chart is filled, so the energy front reads as the colour edge.
// Opaque: the field covers everything.
const THETA_E_UNIT_STOPS: Stop[] = [
  [0.0, 40, 40, 120, 245],
  [0.15, 50, 90, 170, 245],
  [0.3, 60, 150, 180, 245],
  [0.45, 110, 190, 140, 245],
  [0.6, 200, 215, 100, 245],
  [0.72, 245, 190, 70, 245],
  [0.85, 235, 120, 50, 245],
  [1.0, 170, 30, 50, 245],
];

// 2 m dew point, read as moisture rather than as heat: arid air in the
// browns of bare ground, the middle in paper tones, humid air in greens
// deepening through teal to a saturated tropical blue at 25 °C and above.
// The same convention as the relative humidity ramp, so the two moisture
// fields agree on what "dry" looks like. Opaque like temperature.
const DEW_POINT_STOPS: Stop[] = [
  [DEW_POINT_CHART_RANGE[0], 110, 72, 36, 240],
  [-20, 150, 108, 66, 240],
  [-10, 190, 156, 108, 240],
  [0, 222, 208, 168, 240],
  [5, 214, 222, 178, 240],
  [10, 168, 210, 150, 245],
  [15, 110, 190, 140, 245],
  [20, 60, 160, 150, 250],
  [25, 40, 110, 160, 255],
  [DEW_POINT_CHART_RANGE[1], 40, 60, 140, 255],
];

// Visibility in kilometres, painted where it is *reduced*: clear air above
// ten kilometres is the map, haze from ten down to five comes in as a
// translucent straw, mist below five as amber, then orange, red and violet
// as fog closes in under two, one and half a kilometre — the classes an
// aviation or road chart draws, with the worst the most saturated.
const VISIBILITY_STOPS: Stop[] = [
  [0, 90, 30, 130, 250],
  [0.5, 150, 40, 110, 245],
  [1, 205, 55, 60, 240],
  [2, 235, 120, 50, 225],
  [3, 240, 170, 70, 200],
  [5, 236, 210, 120, 160],
  [8, 230, 225, 170, 90],
  [10, 225, 225, 200, 0],
  [VISIBILITY_CHART_MAX, 225, 225, 200, 0],
];

// Sea ice cover in percent. Open water is the map; the ice edge — 15 %,
// the concentration an ice chart draws its edge at — comes in as a pale
// cyan, and the pack deepens through sky blue to a saturated blue at full
// cover. Blue rather than the white an ice chart paints on a blue sea: the
// same ramp has to read on the light theme's white paper, where white ice
// would vanish, and on the dark slate, where it reads lighter than the
// water either way.
const ICE_COVER_STOPS: Stop[] = [
  [0, 200, 232, 245, 0],
  [10, 200, 232, 245, 0],
  [15, 200, 232, 245, 140],
  [40, 150, 210, 235, 200],
  [70, 100, 175, 225, 240],
  [100, 60, 130, 200, 255],
];

// Sea ice thickness in metres, transparent at zero (open water and land),
// climbing from the pale blue of new ice through the blues of first-year
// ice to the violet of a multi-year floe at the codebook's 5 m.
const ICE_THICKNESS_STOPS: Stop[] = [
  [0, 200, 235, 240, 0],
  [0.1, 200, 235, 240, 120],
  [0.5, 150, 215, 235, 190],
  [1, 100, 185, 225, 225],
  [2, 60, 140, 205, 245],
  [3, 50, 95, 180, 255],
  [4, 70, 50, 150, 255],
  [ICE_THICKNESS_CHART_MAX, 90, 20, 110, 255],
];

// Significant wave height in metres: a calm sea under half a metre is
// nearly the map (and land, 0 in the file, is exactly the map), then the
// ramp climbs the way a marine chart's does — blue through teal and green
// to yellow at 3 m, orange and red through 5 and 6, and the violet of a
// storm sea at the 10 m ceiling.
const WAVE_HEIGHT_STOPS: Stop[] = [
  [0, 20, 60, 100, 0],
  [0.25, 60, 130, 185, 120],
  [0.5, 70, 160, 200, 160],
  [1, 80, 195, 195, 195],
  [1.5, 110, 210, 150, 215],
  [2, 170, 220, 110, 230],
  [3, 240, 215, 80, 240],
  [4, 250, 165, 60, 245],
  [5, 245, 110, 55, 250],
  [6, 225, 60, 70, 255],
  [8, 170, 40, 120, 255],
  [WAVE_HEIGHT_CHART_MAX, 110, 30, 130, 255],
];

// Primary wave period in seconds, cool for a short wind sea and warm for a
// long swell: a period under a second does not occur at sea, so the ramp
// is transparent there and land (0) stays the map.
const WAVE_PERIOD_STOPS: Stop[] = [
  [0, 90, 120, 170, 0],
  [1, 90, 120, 170, 150],
  [4, 90, 160, 200, 190],
  [8, 110, 200, 180, 215],
  [12, 200, 210, 110, 235],
  [16, 245, 170, 70, 250],
  [WAVE_PERIOD_CHART_MAX, 225, 80, 60, 255],
];

// Aerosol optical depth at 550 nm, one ramp for the whole column and for
// every species: clear air under 0.05 is the map, haze comes in as a
// translucent straw and deepens through amber and orange to the brown of
// a dust plume at 1, then a violet at the thickest storms, so the darker
// the thicker, the way an AOD map is read. The codebook is logarithmic,
// so the stops crowd the low end where most of the world's sky is.
const AEROSOL_OPTICAL_DEPTH_STOPS: Stop[] = [
  [0, 245, 225, 150, 0],
  [0.05, 245, 225, 150, 60],
  [0.1, 240, 205, 110, 120],
  [0.2, 235, 175, 75, 170],
  [0.5, 220, 130, 50, 210],
  [1, 175, 85, 35, 235],
  [2, 120, 50, 40, 250],
  [5, 80, 25, 70, 255],
];

// Surface particulate matter in µg/m³, in the US AQI's bands: green while
// the air is good, yellow when moderate, orange when unhealthy for the
// sensitive, red when unhealthy, purple when very unhealthy and maroon
// past hazardous — the colours every air quality index map paints, so a
// reader who knows one knows this. The breakpoints are PM2.5's
// (12 / 35 / 55 / 150 / 250) and PM10's (54 / 154 / 254 / 354 / 424); clean
// air is the map, and a band blends into the next at its breakpoint.
const FINE_PARTICULATE_STOPS: Stop[] = [
  [0, 80, 190, 80, 0],
  [4, 80, 190, 80, 90],
  [12, 120, 200, 70, 160],
  [35, 240, 220, 60, 200],
  [55, 245, 145, 40, 225],
  [150, 225, 50, 50, 245],
  [250, 140, 60, 160, 255],
  [500, 120, 20, 60, 255],
];
const COARSE_PARTICULATE_STOPS: Stop[] = [
  [0, 80, 190, 80, 0],
  [15, 80, 190, 80, 90],
  [54, 120, 200, 70, 160],
  [154, 240, 220, 60, 200],
  [254, 245, 145, 40, 225],
  [354, 225, 50, 50, 245],
  [424, 140, 60, 160, 255],
  [1000, 120, 20, 60, 255],
];

// Primary wave direction, degrees true the waves come from, on a hue wheel
// that closes: north red, east yellow, south cyan, west blue, north red
// again. Not a fill the rail offers (levels.ts) — the file's 0 is both
// north and land, so the first code alone is left transparent; a coast
// blends through the wheel. Reachable by URL for a look at the data.
const WAVE_DIRECTION_STOPS: Stop[] = [
  [0, 220, 60, 60, 0],
  [1.5, 220, 60, 60, 255],
  [90, 230, 200, 60, 255],
  [180, 60, 190, 200, 255],
  [270, 70, 90, 210, 255],
  [360, 220, 60, 60, 255],
];

function interpolate(stops: Stop[], value: number): [number, number, number, number] {
  const first = stops[0]!;
  const last = stops[stops.length - 1]!;
  if (value <= first[0]) return [first[1], first[2], first[3], first[4]];
  if (value >= last[0]) return [last[1], last[2], last[3], last[4]];
  for (let index = 1; index < stops.length; index += 1) {
    const upper = stops[index]!;
    if (value <= upper[0]) {
      const lower = stops[index - 1]!;
      const t = (value - lower[0]) / (upper[0] - lower[0]);
      return [
        Math.round(lower[1] + (upper[1] - lower[1]) * t),
        Math.round(lower[2] + (upper[2] - lower[2]) * t),
        Math.round(lower[3] + (upper[3] - lower[3]) * t),
        Math.round(lower[4] + (upper[4] - lower[4]) * t),
      ];
    }
  }
  return [last[1], last[2], last[3], last[4]];
}

export function decodeLinear(quantization: LinearQuantization, code: number): number | null {
  if (code === quantization.nodataCode || code > quantization.maximumCode) return null;
  return quantization.offset + code * quantization.scale;
}

export function decodeLog(quantization: LogQuantization, code: number): number | null {
  if (code === quantization.nodataCode) return null;
  if (code === quantization.zeroCode) return 0;
  if (code > quantization.overflowCode) return null;
  // The overflow code extends the logarithmic grid one step past the maximum.
  const lo = Math.log1p(quantization.trace / quantization.scale);
  const hi = Math.log1p(quantization.maximum / quantization.scale);
  const unit = (code - quantization.minimumCode) / (quantization.maximumCode - quantization.minimumCode);
  return quantization.scale * Math.expm1(lo + unit * (hi - lo));
}

/** The code a logarithmic codebook holds a value at — the encoder's
 * rounding, so a legend's range lands on the codes the bar spans. Zero and
 * anything under the trace take the zero code; a value that rounds past
 * the last code takes the overflow code (the maximum itself, decoded and
 * encoded again, lands back on the last code). */
export function encodeLog(quantization: LogQuantization, value: number): number {
  if (!(value >= quantization.trace)) return quantization.zeroCode;
  const lo = Math.log1p(quantization.trace / quantization.scale);
  const hi = Math.log1p(quantization.maximum / quantization.scale);
  const span = quantization.maximumCode - quantization.minimumCode;
  const unit = (Math.log1p(value / quantization.scale) - lo) / (hi - lo);
  const code = quantization.minimumCode + Math.floor(span * unit + 0.5);
  return code > quantization.maximumCode ? quantization.overflowCode : code;
}

/** The speed field's own transparency, by speed: the ramp above was drawn
 * for particles over a bare map and is opaque, but as a filled field it has
 * to let the ground read through — a calm sea is mostly map, and even a gale
 * keeps a little of it, the way the precipitation palette does. */
const WIND_FIELD_ALPHA: [number, number][] = [
  [0, 0.4],
  [10, 0.55],
  [20, 0.7],
  [40, 0.8],
];

function windFieldAlpha(speed: number): number {
  const first = WIND_FIELD_ALPHA[0]!;
  const last = WIND_FIELD_ALPHA[WIND_FIELD_ALPHA.length - 1]!;
  if (speed <= first[0]) return first[1];
  if (speed >= last[0]) return last[1];
  for (let index = 1; index < WIND_FIELD_ALPHA.length; index += 1) {
    const [upperSpeed, upperAlpha] = WIND_FIELD_ALPHA[index]!;
    if (speed > upperSpeed) continue;
    const [lowerSpeed, lowerAlpha] = WIND_FIELD_ALPHA[index - 1]!;
    return lowerAlpha + ((speed - lowerSpeed) / (upperSpeed - lowerSpeed)) * (upperAlpha - lowerAlpha);
  }
  return last[1];
}

/** The wind ramp as colour stops in m/s with the field's transparency folded
 * in, stretched so the last stop stands for `maxSpeed` — the gust layer's
 * ramp, a scalar the shader looks up by value like any other. */
export function windFieldStops(maxSpeed = WIND_SPEED_MAX): Stop[] {
  const stretch = maxSpeed / WIND_SPEED_MAX;
  return remapStops(WIND_SPEED_STOPS, [0, WIND_SPEED_MAX], [0, maxSpeed]).map(
    ([speed, r, g, b, a]) => [speed, r, g, b, Math.round(a * windFieldAlpha(speed / stretch))] as Stop,
  );
}

/** The same ramp as a filled field: indexed by speed like the particle
 * palette, with the field's transparency folded in. `maxSpeed` is the
 * speed the last entry stands for; the ramp and its transparency stretch
 * with it, so an isobaric wind with a higher ceiling keeps the same colours
 * at the same fraction of its own scale. The 100 m pair is a wind at hub
 * height and takes the 10 m wind's own 40 m/s ceiling (`levels.ts::
 * vectorMaxMagnitude`), so a speed is the same colour on both. */
export function buildWindFieldPalette(maxSpeed = WIND_SPEED_MAX): Uint8Array {
  const palette = buildWindSpeedPalette(maxSpeed);
  const stretch = maxSpeed / WIND_SPEED_MAX;
  for (let index = 0; index < 256; index += 1) {
    const speed = (index / 255) * maxSpeed;
    palette[index * 4 + 3] = Math.round(palette[index * 4 + 3]! * windFieldAlpha(speed / stretch));
  }
  return palette;
}

/** Build the 256x1 RGBA speed palette for the wind particle layer: index i
 * maps speed (i / 255) * maxSpeed m/s to a color; faster wind above the
 * ramp ceiling keeps the last color. */
export function buildWindSpeedPalette(maxSpeed = WIND_SPEED_MAX): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  const stops = maxSpeed === WIND_SPEED_MAX ? WIND_SPEED_STOPS : remapStops(WIND_SPEED_STOPS, [0, WIND_SPEED_MAX], [0, maxSpeed]);
  for (let index = 0; index < 256; index += 1) {
    const speed = (index / 255) * maxSpeed;
    palette.set(interpolate(stops, speed), index * 4);
  }
  return palette;
}

/** A magnitude palette indexed like the wind field's: entry i is the
 * magnitude (i / 255) * maxMagnitude on the given ramp. */
function buildMagnitudePalette(stops: Stop[], maxMagnitude: number): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let index = 0; index < 256; index += 1) {
    palette.set(interpolate(stops, (index / 255) * maxMagnitude), index * 4);
  }
  return palette;
}

/** The vapour flux magnitude palette. */
export function buildVapourFluxPalette(maxMagnitude: number): Uint8Array {
  return buildMagnitudePalette(VAPOUR_FLUX_STOPS, maxMagnitude);
}

/** The wave vector's magnitude palette: its magnitude is the significant
 * wave height, so the ramp is the height's own — land, (0, 0) in the pair,
 * is the map, and a storm sea saturates at the chart ceiling. */
export function buildWaveFieldPalette(maxHeight: number): Uint8Array {
  return buildMagnitudePalette(WAVE_HEIGHT_STOPS, maxHeight);
}

/** A CSS gradient reading a palette top-down — the legend bar of a field
 * whose key is not hand-written in the stylesheet. `from`/`to` pick the index
 * range the legend spans (a codebook's valid codes, or the whole magnitude
 * scale). */
export function legendGradient(palette: Uint8Array, from = 0, to = 255, samples = 12): string {
  const stops: string[] = [];
  for (let sample = 0; sample < samples; sample += 1) {
    const index = Math.round(to - (sample / (samples - 1)) * (to - from));
    const at = index * 4;
    stops.push(`rgba(${palette[at]}, ${palette[at + 1]}, ${palette[at + 2]}, ${(palette[at + 3]! / 255).toFixed(3)})`);
  }
  return `linear-gradient(to bottom, ${stops.join(", ")})`;
}

/** The codebook's own coverage, in the variable's unit — the range an
 * unrecognized field's ramp is spread over. */
function codebookRange(quantization: LinearQuantization): readonly [number, number] {
  return [quantization.offset, quantization.offset + quantization.scale * quantization.maximumCode];
}

/** Color stops for one variable's physical values, chosen by what the field
 * *is* (identity.ts) rather than by what it is called: dswrf, cref, cloud
 * cover and CAPE carry their own ramps, the gust borrows the wind speed
 * ramp, the pressure family reads its codebook, and the isobaric fills take
 * theirs from the family and the level. A field this build does not
 * recognize gets the temperature ramp spread over its own codebook, so it
 * is at least legible — or, on a logarithmic codebook, the precipitation
 * ramp, the one ramp drawn in that shape before the aerosols. */
function stopsFor(variable: BundleVariable, identity: VariableIdentity | null): Stop[] {
  const linear = variable.quantization.type === "linear" ? variable.quantization : null;
  if (identity === null) {
    return linear ? remapStops(TEMPERATURE_STOPS, [-60, 50], codebookRange(linear)) : PRECIPITATION_STOPS;
  }
  const { family, level } = identity;
  if (family === "prate") return PRECIPITATION_STOPS;
  if (aerosolChart(family)) {
    if (family === "pm25") return FINE_PARTICULATE_STOPS;
    if (family === "pm10" || family === "pm10dust") return COARSE_PARTICULATE_STOPS;
    return AEROSOL_OPTICAL_DEPTH_STOPS;
  }
  if (family === "dswrf") return SOLAR_STOPS;
  if (family === "cref") return REFLECTIVITY_STOPS;
  if (family === "gust") return windFieldStops(GUST_SPEED_MAX);
  if (family === "tcdc" || family === "lcdc" || family === "mcdc" || family === "hcdc") return CLOUD_STOPS;
  if (family === "cape") return CAPE_STOPS;
  if (family === "cin") return CIN_STOPS;
  if (family === "pwat") return PWAT_STOPS;
  if (family === "hpbl") return PBL_STOPS;
  // Precipitation type is categorical: the palette steps on its class codes
  // and its legend is a swatch key (`levels.ts::PTYPE_CLASSES`).
  if (family === "ptype") return PTYPE_STOPS;
  if (family === "vis") return VISIBILITY_STOPS;
  if (family === "dpt2m") return DEW_POINT_STOPS;
  // The apparent temperature is a temperature: the same ramp, so 30 °C
  // "feels like" is the same orange as 30 °C is.
  if (family === "aptmp2m") return TEMPERATURE_STOPS;
  if (family === "vvel") return OMEGA_STOPS;
  if (family === "thetae") return remapStops(THETA_E_UNIT_STOPS, [0, 1], thetaEPaletteDomain(level));
  // The skin temperature is a temperature: the 2 m ramp, so the sea
  // surface reads in the same colours as the air over it.
  if (family === "tmpsfc") return TEMPERATURE_STOPS;
  if (family === "icec") return ICE_COVER_STOPS;
  if (family === "icetk") return ICE_THICKNESS_STOPS;
  if (family === "htsgw") return WAVE_HEIGHT_STOPS;
  if (family === "perpw") return WAVE_PERIOD_STOPS;
  if (family === "dirpw") return WAVE_DIRECTION_STOPS;
  if (family === "ir104") return BRIGHTNESS_TEMPERATURE_STOPS;
  if (family === "dustcf") return DUST_CONFIDENCE_STOPS;
  if (family === "hgt" && linear) return pressureStops(linear);
  if (family === "tmp") return remapStops(TEMPERATURE_STOPS, [-60, 50], temperaturePaletteDomain(level));
  if (family === "rh") return HUMIDITY_STOPS;
  if (family === "spfh") {
    const range = isobaricRange("spfh", level);
    if (range) return remapStops(SPECIFIC_HUMIDITY_UNIT_STOPS, [0, 1], [0, range[1]]);
  }
  return linear ? TEMPERATURE_STOPS : PRECIPITATION_STOPS;
}

/** One code in a variable's own codebook, or null for the reserved codes
 * (no data, and anything past the codebook's maximum). */
export function decodeValue(variable: BundleVariable, code: number): number | null {
  return variable.quantization.type === "linear"
    ? decodeLinear(variable.quantization, code)
    : decodeLog(variable.quantization, code);
}

/** The precipitation ramp's colour at one rate in mm/h — the same stops
 * the continuous palette is built from, so a stepped key that samples this
 * per band is the viewer's own ramp banded, not another palette. */
export function precipitationColor(rate: number): [number, number, number, number] {
  return interpolate(PRECIPITATION_STOPS, rate);
}

/** Build the 256x1 RGBA palette texture for one variable's code space.
 *
 * `identity` is what the field is, from its parameter block; omit it and the
 * id string is read as the naming convention instead, which is all a poster
 * (metadata without an open session) has. An explicit `null` means the field
 * is not one this build recognizes. */
export function buildPalette(
  variable: BundleVariable,
  identity: VariableIdentity | null = identityForBundleId(variable.id),
): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  const stops = stopsFor(variable, identity);
  for (let code = 0; code < 256; code += 1) {
    let color: [number, number, number, number] = [0, 0, 0, 0];
    const value = decodeValue(variable, code);
    if (value !== null) {
      // A logarithmic codebook's zero code is dry — or clean — and is not
      // painted.
      if (variable.quantization.type === "linear" || value > 0) color = interpolate(stops, value);
    }
    palette.set(color, code * 4);
  }
  return palette;
}
