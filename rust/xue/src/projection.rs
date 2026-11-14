//! Grid geometry a reader needs without the encoder: the Lambert conformal
//! projection a `grid.projection` block declares, and the true latitudes of a
//! `grid.gaussian` block (`docs/bundle.md`, "Regional Grids").
//!
//! The trigonometry runs through `f64`'s methods, the C library's
//! `sin`/`cos`/`tan`/`pow`, which is what Python's `math` module calls too, so
//! the two encoders land on the same doubles on one machine.

/// A Lambert conformal conic projection on a sphere, as GRIB2 grid
/// definition template 3.30 declares it and as GDAL reports it back
/// (`Lambert Conic Conformal (2SP)` with a spherical datum). Snyder (1987)
/// §15, spherical case; with two equal standard parallels the cone constant
/// is `sin` of the parallel.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LambertConformal {
    /// Sphere radius in metres — 6371229 for every NCEP product.
    pub radius: f64,
    pub standard_parallel_1: f64,
    pub standard_parallel_2: f64,
    pub latitude_of_origin: f64,
    pub central_meridian: f64,
}

impl LambertConformal {
    pub fn cone_constant(&self) -> f64 {
        let phi_1 = self.standard_parallel_1.to_radians();
        let phi_2 = self.standard_parallel_2.to_radians();
        if phi_1 == phi_2 {
            return phi_1.sin();
        }
        (phi_1.cos() / phi_2.cos()).ln()
            / ((std::f64::consts::PI / 4.0 + phi_2 / 2.0).tan()
                / (std::f64::consts::PI / 4.0 + phi_1 / 2.0).tan())
            .ln()
    }

    /// `R·F` — the radius of the parallel at latitude `φ` is
    /// `scale / tan(π/4 + φ/2)^n`.
    pub fn scale(&self) -> f64 {
        let n = self.cone_constant();
        let phi_1 = self.standard_parallel_1.to_radians();
        self.radius * (phi_1.cos() * (std::f64::consts::PI / 4.0 + phi_1 / 2.0).tan().powf(n) / n)
    }

    /// Projected distance from the apex of the cone to the parallel at
    /// `latitude` (degrees).
    pub fn parallel_radius(&self, latitude: f64) -> f64 {
        self.scale()
            / (std::f64::consts::PI / 4.0 + latitude.to_radians() / 2.0)
                .tan()
                .powf(self.cone_constant())
    }

    pub fn origin_radius(&self) -> f64 {
        self.parallel_radius(self.latitude_of_origin)
    }

    /// The angle, in radians, the meridian at `longitude` makes with the
    /// central meridian on the cone.
    pub fn meridian_angle(&self, longitude: f64) -> f64 {
        self.cone_constant() * (longitude - self.central_meridian).to_radians()
    }

    /// Projected `(x, y)` in metres of one point.
    pub fn forward(&self, longitude: f64, latitude: f64) -> (f64, f64) {
        let rho = self.parallel_radius(latitude);
        let theta = self.meridian_angle(longitude);
        (rho * theta.sin(), self.origin_radius() - rho * theta.cos())
    }

    /// [`Self::forward`] for a longitude spelled any way a caller may spell
    /// it. Without the wrap, 262.5 and -97.5 would open different angles on
    /// the cone and one of them would land off a grid that holds the point.
    pub fn forward_wrapped(&self, longitude: f64, latitude: f64) -> (f64, f64) {
        let delta = (longitude - self.central_meridian + 180.0).rem_euclid(360.0) - 180.0;
        self.forward(self.central_meridian + delta, latitude)
    }

    /// Longitude and latitude, in degrees, of one projected point.
    pub fn inverse(&self, x: f64, y: f64) -> (f64, f64) {
        let n = self.cone_constant();
        let dy = self.origin_radius() - y;
        let rho = (x * x + dy * dy).sqrt().copysign(n);
        // Both arguments carry the cone's sign: on a southern cone (n < 0)
        // the radius is negative, and the angle would land half a turn away.
        let sign = 1.0_f64.copysign(n);
        let theta = (sign * x).atan2(sign * dy);
        let latitude = 2.0 * (self.scale() / rho).powf(1.0 / n).atan() - std::f64::consts::PI / 2.0;
        (
            self.central_meridian + (theta / n).to_degrees(),
            latitude.to_degrees(),
        )
    }
}

/// The latitude in degrees of row `row` of the regular Gaussian grid with `n`
/// latitudes per hemisphere, counted from the north: the `row`-th root of the
/// Legendre polynomial of order `2n`.
///
/// Newton's method from `cos(π(row + 0.75) / (2n + 0.5))` on the three-term
/// recurrence, stopping once a step is below `1e-15` or after 100 steps. The
/// operation order is normative (`docs/zarr-profile.md`, "Coordinates"): a
/// library root finder differs from this by up to 1.8e-12° near the poles,
/// which would move the bytes of a store's `latitude` coordinate.
pub fn gaussian_latitude(n: u32, row: u32) -> f64 {
    let order = 2 * u64::from(n);
    let degree = order as f64;
    let mut x = (std::f64::consts::PI * (f64::from(row) + 0.75) / (degree + 0.5)).cos();
    for _ in 0..100 {
        let (mut previous, mut current) = (1.0_f64, x);
        for k in 2..=order {
            let k = k as f64;
            let next = ((2.0 * k - 1.0) * x * current - (k - 1.0) * previous) / k;
            previous = current;
            current = next;
        }
        let derivative = degree * (x * current - previous) / (x * x - 1.0);
        let step = current / derivative;
        x -= step;
        if step.abs() < 1e-15 {
            break;
        }
    }
    x.asin().to_degrees()
}

/// Every latitude of the Gaussian grid with `n` latitudes per hemisphere,
/// north to south.
pub fn gaussian_latitudes(n: u32) -> Vec<f64> {
    (0..2 * n).map(|row| gaussian_latitude(n, row)).collect()
}

/// A `grid.gaussian` block: which rows of the full Gaussian grid a bundle's
/// rows are, so a crop or a reduced-resolution tier keeps its true latitudes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GaussianRows {
    pub n: u32,
    pub first_row: u32,
    pub row_stride: u32,
}

impl GaussianRows {
    /// The latitude of a bundle row; `None` past the Gaussian grid's last
    /// row, which a validated block never reaches from a row on its grid.
    pub fn latitude(&self, row: u32) -> Option<f64> {
        let full = u64::from(self.first_row) + u64::from(row) * u64::from(self.row_stride);
        (full < 2 * u64::from(self.n)).then(|| gaussian_latitude(self.n, full as u32))
    }

    /// Whether the bundle's `row` is the first or last row of the whole
    /// Gaussian grid, past which only the polar cap lies.
    pub(crate) fn reaches_pole(&self, row: u32, north: bool) -> bool {
        let full = u64::from(self.first_row) + u64::from(row) * u64::from(self.row_stride);
        if north {
            full == 0
        } else {
            full == 2 * u64::from(self.n) - 1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HRRR: LambertConformal = LambertConformal {
        radius: 6371229.0,
        standard_parallel_1: 38.5,
        standard_parallel_2: 38.5,
        latitude_of_origin: 38.5,
        central_meridian: -97.5,
    };

    #[test]
    fn lambert_forward_and_inverse_round_trip() {
        for (longitude, latitude) in [(-97.5, 38.5), (-122.72, 21.14), (-60.9, 47.84), (-134.1, 52.6), (-75.0, 30.0)] {
            let (x, y) = HRRR.forward(longitude, latitude);
            let (back_longitude, back_latitude) = HRRR.inverse(x, y);
            assert!((back_longitude - longitude).abs() < 1e-9, "{longitude}");
            assert!((back_latitude - latitude).abs() < 1e-9, "{latitude}");
        }
    }

    #[test]
    fn the_origin_projects_to_zero_and_the_cone_constant_is_the_parallels_sine() {
        let (x, y) = HRRR.forward(-97.5, 38.5);
        assert!(x.abs() < 1e-6 && y.abs() < 1e-6);
        assert!((HRRR.cone_constant() - 38.5_f64.to_radians().sin()).abs() < 1e-15);
        assert!(HRRR.origin_radius() > 0.0);
    }

    #[test]
    fn a_southern_cone_and_two_parallels_round_trip() {
        let south = LambertConformal {
            standard_parallel_1: -30.0,
            standard_parallel_2: -60.0,
            latitude_of_origin: -45.0,
            central_meridian: 140.0,
            ..HRRR
        };
        assert!(south.cone_constant() < 0.0);
        let (x, y) = south.forward(150.0, -40.0);
        let (longitude, latitude) = south.inverse(x, y);
        assert!((longitude - 150.0).abs() < 1e-9);
        assert!((latitude + 40.0).abs() < 1e-9);
    }

    #[test]
    fn a_wrapped_longitude_lands_on_the_same_point() {
        let (x, y) = HRRR.forward_wrapped(262.5, 38.5);
        assert!(x.abs() < 1e-6 && y.abs() < 1e-6);
        let (x, y) = HRRR.forward_wrapped(-100.0 + 720.0, 40.0);
        let (expected_x, expected_y) = HRRR.forward(-100.0, 40.0);
        assert!((x - expected_x).abs() < 1e-6 && (y - expected_y).abs() < 1e-6);
    }

    /// One unit in the last place of `x` is up to 3.6e-12° of latitude next
    /// to a pole at n = 768, and the C libraries behind the first guess and
    /// the final `asin` differ across platforms, so the goldens are held to a
    /// few of those. That the two encoders agree to the bit on one machine is
    /// the parity tests' to say.
    const GOLDEN_TOLERANCE: f64 = 1e-11;

    fn assert_golden(n: u32, row: u32, expected: f64) {
        let latitude = gaussian_latitude(n, row);
        assert!((latitude - expected).abs() < GOLDEN_TOLERANCE, "n={n} row={row}: {latitude}");
    }

    #[test]
    fn gaussian_latitudes_match_the_goldens() {
        assert_golden(95, 0, 89.27671287810583);
        assert_golden(95, 1, 88.33975425118209);
        assert_golden(95, 2, 87.3972623773714);
        assert_golden(95, 94, 0.4724393175759951);
        assert_golden(95, 95, -0.4724393175759951);
        assert_golden(95, 187, -87.3972623773714);
        assert_golden(95, 188, -88.33975425118209);
        assert_golden(95, 189, -89.27671287810583);
        assert_golden(768, 0, 89.91032453466268);
        assert_golden(768, 1, 89.79415738840316);
        assert_golden(768, 2, 89.67730421235662);
        assert_golden(768, 767, 0.058574679619089555);
        assert_golden(768, 768, -0.058574679619089506);
        assert_golden(768, 1533, -89.67730421235662);
        assert_golden(768, 1534, -89.79415738840316);
        assert_golden(768, 1535, -89.91032453466268);
    }

    #[test]
    fn gaussian_latitudes_run_north_to_south() {
        let latitudes = gaussian_latitudes(95);
        assert_eq!(latitudes.len(), 190);
        assert!(latitudes.windows(2).all(|pair| pair[0] > pair[1]));
    }

    #[test]
    fn gaussian_rows_follow_the_crop_and_the_stride() {
        let rows = GaussianRows { n: 95, first_row: 4, row_stride: 2 };
        assert_eq!(rows.latitude(3), Some(gaussian_latitude(95, 10)));
        assert_eq!(rows.latitude(92), Some(gaussian_latitude(95, 188)));
        assert_eq!(rows.latitude(93), None);
        assert!(!rows.reaches_pole(0, true));
        let whole = GaussianRows { n: 95, first_row: 0, row_stride: 1 };
        assert!(whole.reaches_pole(0, true));
        assert!(whole.reaches_pole(189, false));
    }
}
