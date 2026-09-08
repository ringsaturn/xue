//! Python bindings for the Xue decoder and the experimental native encoder.
//!
//! The decoder is [`Bundle`], the same reader the browser runs through wasm.
//! The encoder is exposed at two levels, both thin:
//!
//! * [`convert_bin`] runs the whole native conversion and hands back the same
//!   report dictionary `xuebuild.binconvert.convert_bin` returns, so a build can be
//!   driven from the existing Python tooling.
//! * The array helpers (`quantize`, `encode_residual`, `decimate`,
//!   `encode_poster`) take and return NumPy arrays through `rust-numpy`, so
//!   individual stages can be A/B-tested against `xuebuild/quantize.py` and
//!   `xuebuild/temporal.py` without running a whole build.
//!
//! Errors surface as `RuntimeError` carrying the encoder's own message, the
//! same text the Python CLI prints after `error: `.

use std::path::PathBuf;

use numpy::{PyArray1, PyReadonlyArray1, ToPyArray};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use xue::encode::convert::{convert_bin as native_convert, ConvertOptions};
use xue::encode::gdalio::info_json;
use xue::encode::grid::GridInfo;
use xue::encode::poster::encode_poster as native_encode_poster;
use xue::encode::quantize::codebook;
use xue::encode::temporal::encode_residual as native_encode_residual;

fn to_py_error(error: xue::encode::EncodeError) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

/// Turn a `serde_json::Value` into the equivalent Python object, so the report
/// reads like the dictionary the Python encoder returns rather than a string.
fn to_python(python: Python<'_>, value: &serde_json::Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        serde_json::Value::Null => python.None(),
        serde_json::Value::Bool(inner) => inner.into_pyobject(python)?.to_owned().into(),
        serde_json::Value::Number(inner) => {
            if let Some(integer) = inner.as_i64() {
                integer.into_pyobject(python)?.into_any().unbind()
            } else {
                inner
                    .as_f64()
                    .unwrap_or(f64::NAN)
                    .into_pyobject(python)?
                    .into_any()
                    .unbind()
            }
        }
        serde_json::Value::String(inner) => inner.into_pyobject(python)?.into_any().unbind(),
        serde_json::Value::Array(items) => {
            let list = pyo3::types::PyList::empty(python);
            for item in items {
                list.append(to_python(python, item)?)?;
            }
            list.into_any().unbind()
        }
        serde_json::Value::Object(entries) => {
            let mapping = PyDict::new(python);
            for (key, item) in entries {
                mapping.set_item(key, to_python(python, item)?)?;
            }
            mapping.into_any().unbind()
        }
    })
}

/// A complete `.xue` file, opened and validated.
///
/// The same reader the browser runs through wasm, so a bundle that decodes
/// here decodes there. Every plane comes back as a `uint8` NumPy array of
/// `width * height` quantized codes — what the shader uploads to an R8
/// texture — which the metadata's `quantization` block turns back into
/// physical values.
///
/// The file is held in memory. Reading a 75 MB wind bundle to decode one
/// frame works, but the streaming reader in the Rust crate is what a client
/// that only wants a few frames should use.
#[pyclass]
struct Bundle {
    inner: xue::Bundle,
}

#[pymethods]
impl Bundle {
    /// Open a bundle from its bytes, validating the whole structure.
    #[new]
    fn new(data: &[u8]) -> PyResult<Self> {
        xue::Bundle::open(data)
            .map(|inner| Self { inner })
            .map_err(|error| PyValueError::new_err(error.0))
    }

    /// Open a bundle from a path.
    #[staticmethod]
    fn open(path: PathBuf) -> PyResult<Self> {
        let data = std::fs::read(&path)?;
        Self::new(&data)
    }

    /// The metadata block, parsed. `docs/format.md` is the normative
    /// description of what is in it.
    #[getter]
    fn metadata(&self, python: Python<'_>) -> PyResult<Py<PyAny>> {
        let value: serde_json::Value = serde_json::from_str(self.inner.metadata_json())
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        to_python(python, &value)
    }

    /// The metadata block as it is stored, without a parse.
    #[getter]
    fn metadata_json(&self) -> &str {
        self.inner.metadata_json()
    }

    /// The container's registered variable ids present in this bundle: 1
    /// tmp2m, 2 prate, 3/4 the wind components, 5 dswrf, 6 cref.
    #[getter]
    fn variable_ids(&self) -> Vec<u8> {
        self.inner.variable_ids().to_vec()
    }

    /// Every frame offset on the bundle's time axis, in order. An offset is
    /// worth `unit_seconds` seconds from the run time — for every forecast
    /// source that makes it the forecast hour.
    #[getter]
    fn frame_offsets(&self) -> Vec<u16> {
        self.inner.frame_offsets().to_vec()
    }

    /// Seconds one frame offset is worth. 3600 for every forecast source; the
    /// radar mosaic publishes every six minutes and says 360.
    #[getter]
    fn unit_seconds(&self) -> u32 {
        self.inner.unit_seconds()
    }

    #[getter]
    fn frame_count(&self) -> u32 {
        self.inner.frame_count()
    }

    /// Points per plane, `grid.width * grid.height`.
    #[getter]
    fn plane_length(&self) -> usize {
        self.inner.plane_length()
    }

    /// Decode one plane to its quantized codes.
    ///
    /// Reconstruction follows the predictor recorded in the index — a RAW
    /// plane, or a residual added to its anchor — and the result is checked
    /// against the CRC-32 the encoder wrote, so a corrupt file raises rather
    /// than returning wrong numbers.
    fn decode<'py>(
        &mut self,
        python: Python<'py>,
        variable_id: u8,
        frame_offset: u16,
    ) -> PyResult<Bound<'py, PyArray1<u8>>> {
        let plane = self
            .inner
            .decode_frame(xue::FrameRequest {
                variable_id,
                frame_offset,
            })
            .map_err(|error| PyValueError::new_err(error.0))?;
        Ok(plane.to_pyarray(python))
    }

    /// Drop the decoded-plane cache. Decoding walks a temporal group, so the
    /// reader keeps the anchor it just reconstructed; this releases it.
    fn clear_cache(&mut self) {
        self.inner.clear_cache();
    }

    /// Decode every plane and validate its checksum and reconstructed CRC.
    /// The cache is cleared between frames to bound verification memory.
    fn verify(&mut self, python: Python<'_>) -> PyResult<()> {
        python.detach(|| {
            let variables = self.inner.variable_ids().to_vec();
            let offsets = self.inner.frame_offsets().to_vec();
            for variable_id in variables {
                for &frame_offset in &offsets {
                    let result = self.inner.decode_frame(xue::FrameRequest { variable_id, frame_offset })
                        .map(|_| ());
                    self.inner.clear_cache();
                    result?;
                }
            }
            Ok::<(), xue::DecodeError>(())
        }).map_err(|error| PyValueError::new_err(error.0))
    }

    fn __repr__(&self) -> String {
        format!(
            "<xue.Bundle {} variable(s), {} frames of {} points>",
            self.inner.variable_ids().len(),
            self.inner.frame_count(),
            self.inner.plane_length()
        )
    }
}

/// Convert gridded input into per-variable Xue bundles.
///
/// Mirrors `xuebuild.binconvert.convert_bin`, minus the video artifacts. Returns
/// the build report as a dictionary.
#[pyfunction]
#[pyo3(signature = (
    inputs,
    output_dir,
    *,
    profile = "quality".to_string(),
    model = "gfs".to_string(),
    zstd_level = None,
    require_complete = false,
    expected_hours = 120,
    manifest_path = None,
    latest_path = None,
    run_id = None,
    force = false,
    skip_variants = false,
    bbox = None,
    bundle_ids = None,
    last_hour = None,
    extract_workers = None,
    compress_workers = None,
    verbose = false,
))]
#[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)]
fn convert_bin(
    python: Python<'_>,
    inputs: Vec<PathBuf>,
    output_dir: PathBuf,
    profile: String,
    model: String,
    zstd_level: Option<i32>,
    require_complete: bool,
    expected_hours: i64,
    manifest_path: Option<PathBuf>,
    latest_path: Option<PathBuf>,
    run_id: Option<String>,
    force: bool,
    skip_variants: bool,
    bbox: Option<(f64, f64, f64, f64)>,
    bundle_ids: Option<Vec<String>>,
    last_hour: Option<i64>,
    extract_workers: Option<usize>,
    compress_workers: Option<usize>,
    verbose: bool,
) -> PyResult<Py<PyAny>> {
    let defaults = ConvertOptions::default();
    let options = ConvertOptions {
        profile,
        model,
        zstd_level: zstd_level.unwrap_or(defaults.zstd_level),
        require_complete,
        expected_hours,
        manifest_path,
        latest_path,
        run_id,
        force,
        skip_variants,
        bbox,
        bundle_ids,
        last_hour,
        extract_workers: extract_workers.unwrap_or(defaults.extract_workers),
        compress_workers: compress_workers.unwrap_or(defaults.compress_workers),
        verbose,
    };
    // The conversion is long and holds no Python objects; releasing the GIL
    // lets a caller keep using the interpreter while it runs.
    let report = python
        .detach(|| native_convert(&inputs, &output_dir, &options))
        .map_err(to_py_error)?;
    to_python(python, &report)
}

/// Quantize one plane of physical values with a registered codebook.
///
/// The NumPy counterpart of `xuebuild.quantize.PROFILES[profile][variable_id]`.
#[pyfunction]
fn quantize<'py>(
    python: Python<'py>,
    profile: &str,
    variable_id: &str,
    values: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let book = codebook(profile, variable_id).map_err(to_py_error)?;
    let values = values.as_slice()?;
    let mut codes = vec![0u8; values.len()];
    book.quantize(values, &mut codes).map_err(to_py_error)?;
    Ok(codes.to_pyarray(python))
}

/// Write one variable from quantized uint8 files without loading the whole run.
///
/// `frames` is a sequence of `(frame_offset, path)` pairs covering exactly the
/// metadata time axis. `metadata_json` must describe one variable and is stored
/// verbatim. Set `grouped=False` for independently compressed RAW frames.
/// Returns the file size in bytes. Input files are never modified; the output
/// is atomically replaced only after successful encoding and validation.
#[pyfunction]
#[pyo3(signature = (path, metadata_json, frames, *, grouped=true, zstd_level=15))]
fn write_quantized_bundle(
    python: Python<'_>,
    path: PathBuf,
    metadata_json: String,
    frames: Vec<(u16, PathBuf)>,
    grouped: bool,
    zstd_level: i32,
) -> PyResult<u64> {
    python.detach(|| {
        xue::encode::quantized::write_quantized_bundle(
            &path, &metadata_json, &frames, grouped, zstd_level,
        )
    }).map_err(to_py_error)
}

/// One-byte modulo-256 wrapping residual, the container's only predictor
/// arithmetic.
#[pyfunction]
fn encode_residual<'py>(
    python: Python<'py>,
    current: PyReadonlyArray1<'py, u8>,
    base: PyReadonlyArray1<'py, u8>,
) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let residual =
        native_encode_residual(current.as_slice()?, base.as_slice()?).map_err(to_py_error)?;
    Ok(residual.to_pyarray(python))
}

/// Half-resolution copy of one quantized plane (rows/columns 0, 2, 4, …).
#[pyfunction]
fn decimate<'py>(
    python: Python<'py>,
    codes: PyReadonlyArray1<'py, u8>,
    width: usize,
    height: usize,
) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let codes = codes.as_slice()?;
    let mut half = Vec::with_capacity(width.div_ceil(2) * height.div_ceil(2));
    for row in (0..height).step_by(2) {
        for column in (0..width).step_by(2) {
            half.push(codes[row * width + column]);
        }
    }
    Ok(half.to_pyarray(python))
}

/// The subset of `gdalinfo -json` that `xuebuild` reads, from the GDAL linked
/// into this wheel.
///
/// `name` is a GDAL connection string, not necessarily a filesystem path:
/// the observation path passes `NETCDF:"file.nc":cref`.
///
/// This exists so a build that converts through the native encoder needs no
/// system GDAL at all — the inspection pass that used to shell out to
/// `gdalinfo` was the last thing that did. Only the keys `xuebuild` consumes
/// are reported: `size`, `geoTransform`, dataset `metadata`, and per-band
/// `band`, `description`, `unit`, `scale`, `offset`, `noDataValue` and
/// `metadata`.
#[pyfunction]
fn gdal_info(python: Python<'_>, name: PathBuf) -> PyResult<Py<PyAny>> {
    let info = info_json(&name).map_err(to_py_error)?;
    to_python(python, &info)
}

/// Encode one quantized plane as a first-frame poster; returns
/// `(payload, width, height)`.
#[pyfunction]
fn encode_poster<'py>(
    codes: PyReadonlyArray1<'py, u8>,
    width: usize,
    height: usize,
) -> PyResult<(Vec<u8>, usize, usize)> {
    // Only the extent matters to the poster encoder; the origin and steps go
    // unread, so a placeholder grid keeps the signature small.
    let grid = GridInfo::new(width, height, -180.0, 90.0, 360.0 / width as f64, -1.0);
    let (payload, poster_grid) =
        native_encode_poster(codes.as_slice()?, &grid).map_err(to_py_error)?;
    Ok((payload, poster_grid.width, poster_grid.height))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__doc__", "Xue bundle decoder and experimental native encoder")?;
    module.add_class::<Bundle>()?;
    module.add_function(wrap_pyfunction!(convert_bin, module)?)?;
    module.add_function(wrap_pyfunction!(quantize, module)?)?;
    module.add_function(wrap_pyfunction!(encode_residual, module)?)?;
    module.add_function(wrap_pyfunction!(decimate, module)?)?;
    module.add_function(wrap_pyfunction!(encode_poster, module)?)?;
    module.add_function(wrap_pyfunction!(gdal_info, module)?)?;
    module.add_function(wrap_pyfunction!(write_quantized_bundle, module)?)?;
    Ok(())
}
