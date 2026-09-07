//! Python bindings for the experimental native encoder.
//!
//! Two levels are exposed, both thin:
//!
//! * [`convert_bin`] runs the whole native conversion and hands back the same
//!   report dictionary `xue.binconvert.convert_bin` returns, so a build can be
//!   driven from the existing Python tooling.
//! * The array helpers (`quantize`, `encode_residual`, `decimate`,
//!   `encode_poster`) take and return NumPy arrays through `rust-numpy`, so
//!   individual stages can be A/B-tested against `xue/quantize.py` and
//!   `xue/temporal.py` without running a whole build.
//!
//! Errors surface as `RuntimeError` carrying the encoder's own message, the
//! same text the Python CLI prints after `error: `.

use std::path::PathBuf;

use numpy::{PyArray1, PyReadonlyArray1, ToPyArray};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use xue::encode::convert::{convert_bin as native_convert, ConvertOptions};
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

/// Convert gridded input into per-variable Xue bundles.
///
/// Mirrors `xue.binconvert.convert_bin`, minus the video artifacts. Returns
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
/// The NumPy counterpart of `xue.quantize.PROFILES[profile][variable_id]`.
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
fn xue_encode_py(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__doc__", "Experimental native Xue encoder")?;
    module.add_function(wrap_pyfunction!(convert_bin, module)?)?;
    module.add_function(wrap_pyfunction!(quantize, module)?)?;
    module.add_function(wrap_pyfunction!(encode_residual, module)?)?;
    module.add_function(wrap_pyfunction!(decimate, module)?)?;
    module.add_function(wrap_pyfunction!(encode_poster, module)?)?;
    Ok(())
}
