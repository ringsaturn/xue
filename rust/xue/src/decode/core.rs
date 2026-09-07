//! The decode engine: payload residency, decompression and residual replay.
//!
//! One [`Core`] serves both readers. What differs between a whole file and a
//! streamed one is only where a payload's bytes come from, which is what
//! [`PayloadStore`] abstracts; the dependency walk, the CRC32 check and the
//! code-range check above it are the same code either way.

use std::collections::HashMap;
use std::io::Read;

use crate::decode::structure::Structure;
use crate::format::{crc32, err, Compression, DecodeError, FrameRequest, PlaneEntry, Predictor};

/// Where payload bytes live: the whole file, or per-entry sparse buffers
/// filled in by [`crate::StreamingBundle::insert_range`].
pub(crate) enum PayloadStore {
    Full(Vec<u8>),
    Sparse { payloads: Vec<Option<Vec<u8>>>, resident_bytes: u64 },
}

impl PayloadStore {
    pub(crate) fn payload(&self, position: usize, entry: &PlaneEntry) -> Result<&[u8], DecodeError> {
        match self {
            PayloadStore::Full(data) => {
                let start = entry.data_offset as usize;
                Ok(&data[start..start + entry.compressed_length as usize])
            }
            PayloadStore::Sparse { payloads, .. } => payloads[position]
                .as_deref()
                .ok_or_else(|| err("payload is not resident yet")),
        }
    }

    pub(crate) fn is_resident(&self, position: usize, entry: &PlaneEntry) -> bool {
        match self {
            PayloadStore::Full(_) => true,
            PayloadStore::Sparse { payloads, .. } => {
                entry.compressed_length == 0 || payloads[position].is_some()
            }
        }
    }
}

/// Shared decode engine over a [`Structure`] and a [`PayloadStore`].
pub(crate) struct Core {
    pub(crate) structure: Structure,
    pub(crate) store: PayloadStore,
    pub(crate) anchor_cache: HashMap<FrameRequest, Vec<u8>>,
    pub(crate) output: Vec<u8>,
}

impl Core {
    fn entry(&self, request: FrameRequest) -> Result<(usize, PlaneEntry), DecodeError> {
        let position = self.structure.entry_position(request)?;
        Ok((position, self.structure.entries[position]))
    }

    fn decompress_payload(&self, position: usize, entry: &PlaneEntry) -> Result<Vec<u8>, DecodeError> {
        let raw = self.store.payload(position, entry)?;
        let expected = entry.decoded_length as usize;
        match entry.compression {
            Compression::None => {
                if raw.len() != expected {
                    return Err(err("uncompressed payload length mismatch"));
                }
                Ok(raw.to_vec())
            }
            Compression::ZstdDict => Err(err("ZSTD_DICT payloads require an embedded dictionary decoder")),
            Compression::Zstd => {
                let mut decoder = ruzstd::decoding::StreamingDecoder::new(raw)
                    .map_err(|error| err(format!("zstd frame error: {error}")))?;
                let mut output = Vec::with_capacity(expected);
                let mut limited = decoder.by_ref().take(expected as u64 + 1);
                limited
                    .read_to_end(&mut output)
                    .map_err(|error| err(format!("zstd decode error: {error}")))?;
                if output.len() != expected {
                    return Err(err("decompressed payload length mismatch"));
                }
                Ok(output)
            }
        }
    }

    fn checked_plane(&self, entry: &PlaneEntry, plane: Vec<u8>) -> Result<Vec<u8>, DecodeError> {
        if crc32(&plane) != entry.crc32 {
            return Err(err(format!(
                "plane CRC32 mismatch for variable {} hour {}",
                entry.variable_id, entry.frame_offset
            )));
        }
        let (minimum, maximum) = plane
            .iter()
            .fold((u8::MAX, u8::MIN), |(low, high), &value| (low.min(value), high.max(value)));
        if minimum != entry.minimum_code || maximum != entry.maximum_code {
            return Err(err("plane code range mismatch"));
        }
        Ok(plane)
    }

    fn decode_base(&mut self, request: FrameRequest) -> Result<Vec<u8>, DecodeError> {
        let (position, entry) = self.entry(request)?;
        let plane = match entry.predictor {
            Predictor::Zero => vec![0u8; entry.decoded_length as usize],
            Predictor::Raw => self.decompress_payload(position, &entry)?,
            _ => return Err(err("dependency chain base must be RAW or ZERO")),
        };
        self.checked_plane(&entry, plane)
    }

    /// Decode one frame into an internal buffer and return it as a slice.
    ///
    /// RAW base planes along the dependency chain are cached so scrubbing
    /// inside a temporal group re-decodes only the target residual.
    pub(crate) fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        let chain = self.structure.dependency_chain(request)?;
        let base_request = *chain.last().expect("chain is never empty");
        if chain.len() == 1 {
            // Independent plane: decode directly without growing the cache.
            self.output = self.decode_base(base_request)?;
            return Ok(&self.output);
        }
        if !self.anchor_cache.contains_key(&base_request) {
            let base = self.decode_base(base_request)?;
            self.anchor_cache.insert(base_request, base);
        }
        let mut plane = self.anchor_cache[&base_request].clone();
        for link in chain.iter().rev().skip(1) {
            let (position, entry) = self.entry(*link)?;
            let residual = self.decompress_payload(position, &entry)?;
            if residual.len() != plane.len() {
                return Err(err("residual length mismatch"));
            }
            for (target, delta) in plane.iter_mut().zip(residual.iter()) {
                *target = target.wrapping_add(*delta);
            }
            plane = self.checked_plane(&entry, plane)?;
        }
        self.output = plane;
        Ok(&self.output)
    }
}
