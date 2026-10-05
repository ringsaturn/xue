//! A direct reader for GRIB2 fields packed as PNG (data representation
//! template 5.41), the packing of every MRMS product.
//!
//! GDAL's GRIB driver spends about 0.3 s on one 7000 x 3500 MRMS plane, of
//! which the PNG inflate is under 10 ms; this reads the same plane in about
//! 80 ms. That matters for a rolling observation window, which converts
//! every frame of the window again each round.
//!
//! The values must be GDAL's to the bit, or the native encoder stops
//! matching the reference. g2clib unpacks a value as `refD + bdscale * X`
//! in single precision, and whether the compiler fused that multiply-add
//! depends on how the linked GDAL was built (Apple clang contracts it, a
//! default x86-64 GCC build does not). Which one applies is not guessed: the
//! first plane read in a process is also read through GDAL, and the
//! arithmetic that reproduces it bit for bit is used from then on. If
//! neither does, every read goes through GDAL.
//!
//! Anything other than one field per message, template 5.41 at 8 or 16
//! bits, no bitmap, a grey PNG, and rows west to east (north to south or
//! south to north) is left to GDAL, as is a field GDAL converts to Celsius.

use std::io::Read;
use std::path::Path;
use std::sync::Mutex;

use flate2::read::ZlibDecoder;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::gdalio::Dataset;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Arithmetic {
    Fused,
    Separate,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Calibration {
    Pending,
    Known(Arithmetic),
    Unavailable,
}

static CALIBRATION: Mutex<Calibration> = Mutex::new(Calibration::Pending);

fn calibration() -> Calibration {
    *CALIBRATION.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn settle(state: Calibration) {
    *CALIBRATION.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = state;
}

/// One GRIB file's messages, located once so each band is a slice of the
/// bytes already read.
pub(crate) struct GribPngFile {
    bytes: Vec<u8>,
    /// Per message (band order): its PNG field, or `None` if this reader
    /// does not take it.
    fields: Vec<Option<PngField>>,
}

#[derive(Clone, Debug)]
struct PngField {
    width: usize,
    height: usize,
    south_to_north: bool,
    reference: f32,
    binary_scale: i32,
    decimal_scale: i32,
    bits: u8,
    /// The PNG stream: offset and length within the file.
    png: (usize, usize),
}

impl GribPngFile {
    /// The file's fields, or `None` when it is not a GRIB file (a netCDF
    /// series, say) or is not one this reader can walk.
    pub(crate) fn open(path: &Path) -> Option<Self> {
        let mut head = [0u8; 4];
        std::fs::File::open(path).ok()?.read_exact(&mut head).ok()?;
        if &head != b"GRIB" {
            return None;
        }
        let bytes = std::fs::read(path).ok()?;
        let fields = index_messages(&bytes)?;
        fields.iter().any(Option::is_some).then_some(Self { bytes, fields })
    }

    /// Band `number` (1-based, as GDAL counts) as GDAL would read it, or
    /// GDAL's own read when the direct path cannot vouch for the bits.
    pub(crate) fn read_band_f64(&self, dataset: &Dataset, number: usize) -> Result<Vec<f64>> {
        let field = match self.fields.get(number.wrapping_sub(1)) {
            Some(Some(field)) if (field.width, field.height) == dataset.size() => field,
            _ => return dataset.read_band_f64(number),
        };
        // GDAL turns a Kelvin field into Celsius (GRIB_NORMALIZE_UNITS),
        // which a calibration on some other field would not have seen.
        if dataset.band_info(number)?.item("GRIB_UNIT") == "[C]" {
            return dataset.read_band_f64(number);
        }
        match calibration() {
            Calibration::Known(arithmetic) => self.unpack(field, arithmetic),
            Calibration::Unavailable => dataset.read_band_f64(number),
            Calibration::Pending => {
                let reference = dataset.read_band_f64(number)?;
                let Ok(fused) = self.unpack(field, Arithmetic::Fused) else {
                    settle(Calibration::Unavailable);
                    return Ok(reference);
                };
                let separate = self.unpack(field, Arithmetic::Separate)?;
                let fused_matches = same_bits(&fused, &reference);
                let separate_matches = same_bits(&separate, &reference);
                match (fused_matches, separate_matches) {
                    (true, false) => settle(Calibration::Known(Arithmetic::Fused)),
                    (false, true) => settle(Calibration::Known(Arithmetic::Separate)),
                    (false, false) => settle(Calibration::Unavailable),
                    // A plane on which the two agree (no echo anywhere)
                    // decides nothing; the next one will.
                    (true, true) => {}
                }
                Ok(reference)
            }
        }
    }

    fn unpack(&self, field: &PngField, arithmetic: Arithmetic) -> Result<Vec<f64>> {
        let (offset, length) = field.png;
        let samples = decode_png(&self.bytes[offset..offset + length], field)?;
        // g2clib's pngunpack: every factor rounded to single precision
        // before the multiply-add, which is itself single precision.
        let binary = int_power(2.0, field.binary_scale) as f32;
        let decimal = int_power(10.0, -field.decimal_scale) as f32;
        let scale = binary * decimal;
        let base = field.reference * decimal;
        let value = |sample: u16| -> f64 {
            let x = f32::from(sample);
            match arithmetic {
                Arithmetic::Fused => scale.mul_add(x, base) as f64,
                Arithmetic::Separate => (base + scale * x) as f64,
            }
        };
        let (width, height) = (field.width, field.height);
        let mut values = Vec::with_capacity(width * height);
        for row in 0..height {
            // GDAL hands rows north to south whatever order they were
            // stored in.
            let stored = if field.south_to_north { height - 1 - row } else { row };
            values.extend(samples[stored * width..(stored + 1) * width].iter().map(|&s| value(s)));
        }
        Ok(values)
    }
}

fn same_bits(a: &[f64], b: &[f64]) -> bool {
    a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.to_bits() == y.to_bits())
}

/// g2clib's `int_power`, so a scale factor rounds as GDAL's does.
fn int_power(base: f64, exponent: i32) -> f64 {
    let (mut base, mut exponent) = (base, exponent);
    if exponent < 0 {
        exponent = -exponent;
        base = 1.0 / base;
    }
    let mut value = 1.0;
    while exponent != 0 {
        if exponent & 1 != 0 {
            value *= base;
        }
        base *= base;
        exponent >>= 1;
    }
    value
}

fn be_u32(bytes: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_be_bytes(bytes.get(at..at + 4)?.try_into().ok()?))
}

fn be_u16(bytes: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_be_bytes(bytes.get(at..at + 2)?.try_into().ok()?))
}

/// GRIB2's signed integers are sign and magnitude, not two's complement.
fn signed_16(bytes: &[u8], at: usize) -> Option<i32> {
    let raw = i32::from(be_u16(bytes, at)?);
    Some(if raw & 0x8000 != 0 { -(raw & 0x7fff) } else { raw })
}

/// Every message of the file in order. `None` for the whole file when the
/// framing is broken, or when a message repeats sections (more than one
/// field), which would shift GDAL's band numbers off the message count.
fn index_messages(bytes: &[u8]) -> Option<Vec<Option<PngField>>> {
    let mut fields = Vec::new();
    let mut start = 0;
    while start + 16 <= bytes.len() {
        if &bytes[start..start + 4] != b"GRIB" || bytes[start + 7] != 2 {
            return None;
        }
        let length = usize::try_from(u64::from_be_bytes(bytes[start + 8..start + 16].try_into().ok()?)).ok()?;
        let message = bytes.get(start..start + length)?;
        fields.push(message_field(message, start)?);
        start += length;
    }
    (start == bytes.len()).then_some(fields)
}

/// One message's field: `Some(None)` when it is a single field this reader
/// does not take, `None` when it holds more than one.
fn message_field(message: &[u8], offset: usize) -> Option<Option<PngField>> {
    let mut at = 16;
    let (mut grid, mut representation, mut bitmap, mut data) = (None, None, None, None);
    let mut data_sections = 0;
    while at + 4 <= message.len() && &message[at..at + 4] != b"7777" {
        let length = be_u32(message, at)? as usize;
        if length < 5 || at + length > message.len() {
            return None;
        }
        let section = &message[at..at + length];
        match section[4] {
            3 => grid = Some(section),
            5 => representation = Some(section),
            6 => bitmap = Some(section),
            7 => {
                data = Some((offset + at + 5, length - 5));
                data_sections += 1;
            }
            _ => {}
        }
        at += length;
    }
    if data_sections != 1 {
        return None;
    }
    Some(png_field(grid?, representation?, bitmap?, data?))
}

fn png_field(grid: &[u8], representation: &[u8], bitmap: &[u8], png: (usize, usize)) -> Option<PngField> {
    // Grid template 3.0 (regular latitude/longitude): Ni and Nj at octets
    // 31 and 35, the scanning mode at octet 72.
    if be_u16(grid, 12)? != 0 || grid.len() < 72 {
        return None;
    }
    let width = be_u32(grid, 30)? as usize;
    let height = be_u32(grid, 34)? as usize;
    let south_to_north = match grid[71] {
        0x00 => false,
        0x40 => true,
        _ => return None,
    };
    // Template 5.41: reference value, binary and decimal scale factors,
    // bits per value, at octets 12, 16, 18 and 20.
    if be_u16(representation, 9)? != 41 || representation.len() < 21 {
        return None;
    }
    let bits = representation[19];
    if !matches!(bits, 8 | 16) {
        return None;
    }
    // Bitmap indicator 255: no bitmap, every point carries a value.
    if *bitmap.get(5)? != 255 {
        return None;
    }
    Some(PngField {
        width,
        height,
        south_to_north,
        reference: f32::from_bits(be_u32(representation, 11)?),
        binary_scale: signed_16(representation, 15)?,
        decimal_scale: signed_16(representation, 17)?,
        bits,
        png,
    })
}

fn invalid(message: &str) -> EncodeError {
    EncodeError::conversion(format!("GRIB2 PNG field: {message}"))
}

/// The samples of a grey PNG at the field's bit depth, in stored row order.
fn decode_png(png: &[u8], field: &PngField) -> Result<Vec<u16>> {
    if png.get(..8) != Some(b"\x89PNG\r\n\x1a\n".as_slice()) {
        return Err(invalid("no PNG signature"));
    }
    let mut at = 8;
    let mut header = None;
    let mut compressed = Vec::new();
    while at + 8 <= png.len() {
        let length = be_u32(png, at).ok_or_else(|| invalid("truncated chunk"))? as usize;
        let body = png.get(at + 8..at + 8 + length).ok_or_else(|| invalid("truncated chunk"))?;
        match &png[at + 4..at + 8] {
            b"IHDR" => header = Some(body),
            b"IDAT" => compressed.extend_from_slice(body),
            b"IEND" => break,
            _ => {}
        }
        at += 12 + length;
    }
    let header = header.ok_or_else(|| invalid("no IHDR"))?;
    let (width, height) = (
        be_u32(header, 0).unwrap_or(0) as usize,
        be_u32(header, 4).unwrap_or(0) as usize,
    );
    // Bit depth equal to the field's, colour type 0 (grey), no interlace.
    if (width, height) != (field.width, field.height)
        || header.get(8..13) != Some([field.bits, 0, 0, 0, 0].as_slice())
    {
        return Err(invalid("not a grey PNG of the field's size and depth"));
    }
    let bytes_per_sample = usize::from(field.bits / 8);
    let stride = width * bytes_per_sample;
    let mut filtered = Vec::with_capacity((stride + 1) * height);
    ZlibDecoder::new(compressed.as_slice())
        .read_to_end(&mut filtered)
        .map_err(|error| invalid(&format!("inflate failed: {error}")))?;
    if filtered.len() != (stride + 1) * height {
        return Err(invalid("inflated size does not match the image"));
    }
    let image = unfilter(&filtered, stride, height, bytes_per_sample)?;
    Ok(match bytes_per_sample {
        1 => image.into_iter().map(u16::from).collect(),
        _ => image.chunks_exact(2).map(|pair| u16::from_be_bytes([pair[0], pair[1]])).collect(),
    })
}

/// Undo the PNG row filters (RFC 2083 §6): each row is prefixed by its
/// filter type and predicts every byte from the one `bpp` to its left, the
/// one above, or both.
fn unfilter(filtered: &[u8], stride: usize, height: usize, bpp: usize) -> Result<Vec<u8>> {
    let mut image = vec![0u8; stride * height];
    let zero = vec![0u8; stride];
    for row in 0..height {
        let line = &filtered[row * (stride + 1)..(row + 1) * (stride + 1)];
        let (filter, source) = (line[0], &line[1..]);
        let (done, rest) = image.split_at_mut(row * stride);
        let current = &mut rest[..stride];
        let up = if row == 0 { &zero[..] } else { &done[(row - 1) * stride..] };
        match filter {
            0 => current.copy_from_slice(source),
            1 => {
                current[..bpp].copy_from_slice(&source[..bpp]);
                for x in bpp..stride {
                    current[x] = source[x].wrapping_add(current[x - bpp]);
                }
            }
            2 => {
                for x in 0..stride {
                    current[x] = source[x].wrapping_add(up[x]);
                }
            }
            3 => {
                for x in 0..bpp {
                    current[x] = source[x].wrapping_add(up[x] >> 1);
                }
                for x in bpp..stride {
                    let mean = (u16::from(current[x - bpp]) + u16::from(up[x])) >> 1;
                    current[x] = source[x].wrapping_add(mean as u8);
                }
            }
            4 => {
                for x in 0..bpp {
                    current[x] = source[x].wrapping_add(up[x]);
                }
                for x in bpp..stride {
                    let (a, b, c) = (
                        i16::from(current[x - bpp]),
                        i16::from(up[x]),
                        i16::from(up[x - bpp]),
                    );
                    let (pa, pb, pc) = ((b - c).abs(), (a - c).abs(), (a + b - 2 * c).abs());
                    let predicted = if pa <= pb && pa <= pc {
                        a
                    } else if pb <= pc {
                        b
                    } else {
                        c
                    };
                    current[x] = source[x].wrapping_add(predicted as u8);
                }
            }
            other => return Err(invalid(&format!("unknown row filter {other}"))),
        }
    }
    Ok(image)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn fixture(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures").join(name)
    }

    #[test]
    fn the_paeth_and_average_filters_invert_by_hand() {
        // Two rows of two 16-bit samples: row 0 Sub, row 1 Paeth, then
        // Average and Up over the same data.
        let image: [u8; 8] = [1, 2, 3, 4, 5, 6, 7, 8];
        let sub = [1, 1, 2, 2, 2];
        let paeth = |row: &[u8], up: &[u8]| -> Vec<u8> {
            let mut out = vec![4];
            for x in 0..4 {
                let a = if x >= 2 { i16::from(row[x - 2]) } else { 0 };
                let b = i16::from(up[x]);
                let c = if x >= 2 { i16::from(up[x - 2]) } else { 0 };
                let (pa, pb, pc) = ((b - c).abs(), (a - c).abs(), (a + b - 2 * c).abs());
                let p = if pa <= pb && pa <= pc { a } else if pb <= pc { b } else { c };
                out.push(row[x].wrapping_sub(p as u8));
            }
            out
        };
        let mut filtered = sub.to_vec();
        filtered.extend(paeth(&image[4..], &image[..4]));
        assert_eq!(unfilter(&filtered, 4, 2, 2).unwrap(), image);
        let average: Vec<u8> = [3u8]
            .into_iter()
            .chain((0..4).map(|x| {
                let a = if x >= 2 { u16::from(image[4 + x - 2]) } else { 0 };
                image[4 + x].wrapping_sub(((a + u16::from(image[x])) >> 1) as u8)
            }))
            .collect();
        let mut filtered = vec![0, 1, 2, 3, 4];
        filtered.extend(average);
        assert_eq!(unfilter(&filtered, 4, 2, 2).unwrap(), image);
    }

    #[test]
    fn the_mrms_fixture_reads_as_gdal_reads_it() {
        // Both products of a frame, rows stored south to north.
        let path = fixture("mrms.2026091300.t0000.crop.grib2");
        let file = GribPngFile::open(&path).expect("a PNG-packed GRIB");
        let dataset = Dataset::open(&path).unwrap();
        assert_eq!(file.fields.len(), dataset.band_count());
        let mut decided = false;
        for band in 1..=dataset.band_count() {
            let field = file.fields[band - 1].as_ref().expect("template 5.41");
            assert!(field.south_to_north);
            let gdal = dataset.read_band_f64(band).unwrap();
            let fused = same_bits(&file.unpack(field, Arithmetic::Fused).unwrap(), &gdal);
            let separate = same_bits(&file.unpack(field, Arithmetic::Separate).unwrap(), &gdal);
            assert!(fused || separate, "band {band}: neither arithmetic reproduces GDAL");
            // The fixture must tell the two apart, or the calibration it
            // drives in the parity tests would never leave GDAL.
            decided |= fused != separate;
            assert!(same_bits(&file.read_band_f64(&dataset, band).unwrap(), &gdal));
        }
        assert!(decided, "the fixture does not separate the two arithmetics");
    }

    #[test]
    fn other_packings_are_left_to_gdal() {
        // GFS packs with templates 5.0/5.3, never PNG.
        assert!(GribPngFile::open(&fixture("gfs.2026081406.f000.crop.grib2")).is_none());
        assert!(GribPngFile::open(&fixture("cma.2026091609.crop.nc")).is_none());
    }
}
