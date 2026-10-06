//! Reading one NEXRAD Level 3 digital radial product (N0B, N0G) into a sweep
//! on the fixed 0.5° beam grid.
//!
//! A port of the Python reference `xuebuild/nexrad/level3.py::read_sweep`,
//! which the polar store is built from: the browser reads a live site's
//! newest sweeps straight from the public Level 3 bucket with this, and must
//! accept, refuse and bin exactly what the store's builder does, so a live
//! sweep and a stored one of the same file are the same codes.
//!
//! The layout is the WSR-88D RPG-to-class-1-user ICD (2620001): a WMO
//! abbreviated heading, the 18-byte message header, the 102-byte product
//! description block, and the product symbology block — bzip2-compressed when
//! the description block says so — whose one layer is a packet 16 (digital
//! radial data array). Codes are copied, never requantized: the threshold
//! words must state the codebook `docs/nexrad.md` §2 fixes, or the file is
//! refused. Every offset is bounds-checked, and the only allocations are the
//! decompressed body, capped before it grows, and the code grid, whose size
//! the product code fixes rather than the file.

use std::io::Read;

use crate::format::{err, DecodeError};

/// Beams on the fixed grid; beam `j` covers `[0.5 j, 0.5 (j + 1))` degrees
/// from north.
pub const LEVEL3_BEAMS: usize = 720;
const BEAM_WIDTH: f64 = 0.5;

const HEADER_BYTES: usize = 18;
const DESCRIPTION_BYTES: usize = 102;
const PACKET_DIGITAL_RADIAL: u16 = 16;

/// The symbology block a 720 × 1840 sweep needs is about 1.3 MB; anything
/// past this is not a product this reader accepts, and stopping here keeps a
/// bzip2 bomb from growing the heap.
const MAX_BODY_BYTES: u64 = 16 << 20;

/// Unix seconds of the ICD's day 0, 31 December 1969: day 1 is 1 January 1970.
const EPOCH_DAY_ZERO: i64 = -86_400;

/// One accepted product: `(product code, gates, minimum ×10, increment ×10,
/// levels)`. The threshold words must equal these exactly — −32.0 dBZ /
/// −63.5 m/s, a 0.5 increment and 254 levels over codes 2–255.
const PRODUCTS: [(u16, &str, u32, i16, i16, i16); 2] = [
    (153, "n0b", 1840, -320, 5, 254),
    (154, "n0g", 1200, -635, 5, 254),
];

/// One Level 3 sweep, its header facts, and its codes on the beam grid.
#[derive(Debug, Clone, PartialEq)]
pub struct Level3Sweep {
    /// 153 (N0B) or 154 (N0G).
    pub product_code: u16,
    /// Gates per beam: 1840 for N0B, 1200 for N0G.
    pub gates: u32,
    /// The sweep's own start in Unix seconds (a supplemental SAILS sweep's,
    /// not its volume's).
    pub scan_time: i64,
    /// Degrees.
    pub elevation: f32,
    pub latitude: f64,
    pub longitude: f64,
    pub height_m: f64,
    pub vcp: i16,
    pub volume_number: i16,
    pub elevation_number: i16,
    /// `720 × gates` bytes, beam-major; a beam the file does not carry is
    /// code 0.
    pub codes: Vec<u8>,
}

fn be_i16(data: &[u8], offset: usize) -> Option<i16> {
    let bytes = data.get(offset..offset.checked_add(2)?)?;
    Some(i16::from_be_bytes([bytes[0], bytes[1]]))
}

fn be_u16(data: &[u8], offset: usize) -> Option<u16> {
    be_i16(data, offset).map(|value| value as u16)
}

/// ICD halfword `number` (10 … 60) of the product description block, signed.
/// The block is exactly 102 bytes, so every number in range is in bounds.
fn halfword(block: &[u8], number: usize) -> i16 {
    let offset = (number - 10) * 2;
    i16::from_be_bytes([block[offset], block[offset + 1]])
}

/// The 32-bit value in halfwords `number` and `number + 1`, unsigned.
fn word(block: &[u8], number: usize) -> u32 {
    let offset = (number - 10) * 2;
    u32::from_be_bytes([block[offset], block[offset + 1], block[offset + 2], block[offset + 3]])
}

fn find(data: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    data.get(from..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|position| position + from)
}

/// Offset of the message header: past the two CR CR LF lines of the WMO
/// abbreviated heading when the file starts with four letters, else 0.
fn skip_wmo_heading(data: &[u8]) -> Result<usize, DecodeError> {
    if data.len() < 4 || !data[..4].iter().all(u8::is_ascii_alphabetic) {
        return Ok(0);
    }
    let malformed = || err("Level 3 file has a malformed WMO heading");
    let first = find(data, b"\r\r\n", 0).ok_or_else(malformed)?;
    let second = find(data, b"\r\r\n", first + 3).ok_or_else(malformed)?;
    if second > 64 {
        return Err(malformed());
    }
    Ok(second + 3)
}

/// Parse one Level 3 N0B / N0G file's bytes.
pub fn read_level3(data: &[u8]) -> Result<Level3Sweep, DecodeError> {
    let start = skip_wmo_heading(data)?;
    let description = data
        .get(start + HEADER_BYTES..start + HEADER_BYTES + DESCRIPTION_BYTES)
        .filter(|block| halfword(block, 10) == -1)
        .ok_or_else(|| err("Level 3 file has no product description block"))?;
    let code = halfword(description, 16);
    let &(product_code, product, gates, minimum, increment, levels) = PRODUCTS
        .iter()
        .find(|entry| i32::from(entry.0) == i32::from(code))
        .ok_or_else(|| err(format!("Level 3 product {code} is not one this product carries")))?;
    let thresholds = (halfword(description, 31), halfword(description, 32), halfword(description, 33));
    if thresholds != (minimum, increment, levels) {
        return Err(err(format!(
            "Level 3 {product} thresholds {thresholds:?} are not the codebook (min, step, levels) {:?}",
            (minimum, increment, levels)
        )));
    }
    let days = i64::from(halfword(description, 21));
    let seconds = i64::from(word(description, 22));
    let scan_time = EPOCH_DAY_ZERO + days * 86_400 + seconds;

    let raw = &data[start + HEADER_BYTES + DESCRIPTION_BYTES..];
    let decompressed;
    let body = match halfword(description, 51) {
        0 => raw,
        1 => {
            decompressed = bunzip(raw, product)?;
            &decompressed[..]
        }
        other => return Err(err(format!("Level 3 {product} uses compression method {other}"))),
    };
    let codes = read_radials(body, gates as usize, product)?;
    Ok(Level3Sweep {
        product_code,
        gates,
        scan_time,
        elevation: f32::from(halfword(description, 30)) / 10.0,
        latitude: f64::from(word(description, 11) as i32) / 1000.0,
        longitude: f64::from(word(description, 13) as i32) / 1000.0,
        height_m: f64::from(halfword(description, 15)) * 0.3048,
        vcp: halfword(description, 18),
        volume_number: halfword(description, 20),
        elevation_number: halfword(description, 29),
        codes,
    })
}

/// Every concatenated bzip2 stream, as Python's `bz2.decompress` reads them.
fn bunzip(raw: &[u8], product: &str) -> Result<Vec<u8>, DecodeError> {
    let mut body = Vec::new();
    bzip2::read::MultiBzDecoder::new(raw)
        .take(MAX_BODY_BYTES + 1)
        .read_to_end(&mut body)
        .map_err(|error| err(format!("Level 3 {product} symbology is not bzip2: {error}")))?;
    if body.len() as u64 > MAX_BODY_BYTES {
        return Err(err(format!("Level 3 {product} symbology exceeds {MAX_BODY_BYTES} bytes")));
    }
    Ok(body)
}

/// The symbology block's one packet 16, binned by beam centre onto the fixed
/// 0.5° grid.
fn read_radials(body: &[u8], gates: usize, product: &str) -> Result<Vec<u8>, DecodeError> {
    if body.len() < 16 || be_i16(body, 0) != Some(-1) || be_i16(body, 2) != Some(1) {
        return Err(err(format!("Level 3 {product} has no product symbology block")));
    }
    if be_i16(body, 8) != Some(1) || be_i16(body, 10) != Some(-1) {
        return Err(err(format!("Level 3 {product} symbology must hold exactly one layer")));
    }
    let truncated = || err(format!("Level 3 {product} radial data is truncated"));
    // Past the block header (10 bytes) and the layer header (6).
    let offset = 16;
    let packet = be_u16(body, offset).ok_or_else(truncated)?;
    if packet != PACKET_DIGITAL_RADIAL {
        return Err(err(format!("Level 3 {product} carries packet {packet}, not a digital radial array")));
    }
    let first_bin = be_i16(body, offset + 2).ok_or_else(truncated)?;
    let bins = be_i16(body, offset + 4).ok_or_else(truncated)?;
    let radials = be_i16(body, offset + 12).ok_or_else(truncated)?;
    if first_bin != 0 || i64::from(bins) != gates as i64 {
        return Err(err(format!("Level 3 {product} has {bins} gates from {first_bin}, not {gates} from 0")));
    }
    let mut codes = vec![0u8; LEVEL3_BEAMS * gates];
    let mut filled = [false; LEVEL3_BEAMS];
    let mut position = offset + 14;
    for _ in 0..radials.max(0) {
        let length = be_i16(body, position).ok_or_else(truncated)?;
        let start_angle = be_i16(body, position + 2).ok_or_else(truncated)?;
        let delta_angle = be_i16(body, position + 4).ok_or_else(truncated)?;
        position += 6;
        // A radial's bytes are read as `gates` codes from its start whatever
        // its length says, as the reference reads them; the length only
        // steps to the next radial, and stepping backwards is refused.
        let length = usize::try_from(length).map_err(|_| truncated())?;
        let end = position.checked_add(gates).ok_or_else(truncated)?;
        let values = body.get(position..end).ok_or_else(truncated)?;
        position = position.checked_add(length).ok_or_else(truncated)?;
        let centre = ((f64::from(start_angle) + f64::from(delta_angle) / 2.0) / 10.0).rem_euclid(360.0);
        // Exact: the beam width is a power of two, so the division rounds
        // nothing and floor matches Python's float floor division.
        let beam = ((centre / BEAM_WIDTH).floor() as usize) % LEVEL3_BEAMS;
        if filled[beam] {
            return Err(err(format!("Level 3 {product} has two radials in beam {beam}")));
        }
        filled[beam] = true;
        codes[beam * gates..(beam + 1) * gates].copy_from_slice(values);
    }
    Ok(codes)
}
