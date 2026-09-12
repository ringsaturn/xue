//! A thin safe wrapper over the handful of GDAL C entry points the encoder
//! needs.
//!
//! The Python encoder shells out to `gdalinfo -json` and `gdal_translate -of
//! ENVI -ot Float64`; this reads the same numbers in process, which is the
//! whole point of the experiment — a production GFS run wrote and re-read
//! several gigabytes of intermediate ENVI files per build.
//!
//! Only `gdal-sys` (georust's GDAL FFI crate) is used, not the high-level
//! `gdal` crate: as of `gdal` 0.19 the safe wrapper does not compile against
//! GDAL 3.13, whose `GDALDataType` and `GDALRasterIOExtraArg` changed shape.
//! The surface used here is a dozen stable C functions, so binding them
//! directly costs less than pinning an older GDAL.

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::path::Path;
use std::ptr;
use std::sync::{Mutex, MutexGuard, Once};

use gdal_sys::{
    CPLErr, CPLGetLastErrorMsg, GDALAccess, GDALAllRegister, GDALClose, GDALDataType,
    GDALDatasetH, GDALGetDescription, GDALGetGeoTransform, GDALGetMetadata, GDALGetRasterBand,
    GDALGetRasterCount, GDALGetRasterNoDataValue, GDALGetRasterOffset, GDALGetRasterScale,
    GDALGetRasterUnitType, GDALGetRasterXSize, GDALGetRasterYSize, GDALGetSpatialRef, GDALOpen,
    GDALRWFlag, GDALRasterIO, OSRExportToWktEx, VSIFree,
};

use crate::encode::errors::{EncodeError, Result};

static REGISTER: Once = Once::new();

/// Serializes access to GDAL's netCDF driver.
///
/// libnetcdf and the HDF5 library under it are not thread-safe, and GDAL does
/// not lock for them: opening and reading one file from several threads at
/// once fails with "netCDF chunk fetch failed: NetCDF: HDF error". The Python
/// encoder never met this because every extraction was its own
/// `gdal_translate` process. GRIB reads are unaffected and stay parallel.
static NETCDF: Mutex<()> = Mutex::new(());

/// Whether a GDAL connection string names a dataset that must be read under
/// [`netcdf_guard`]. NetCDF subdatasets are spelled `NETCDF:"<file>":<var>`.
pub fn needs_serial_access(name: &Path) -> bool {
    name.to_string_lossy().starts_with("NETCDF:")
}

/// Hold this for as long as a netCDF dataset is open and being read.
pub fn netcdf_guard() -> MutexGuard<'static, ()> {
    // A poisoned lock only means some other worker failed mid-read; the driver
    // itself is no worse off, and the error has already been reported.
    NETCDF.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn register() {
    REGISTER.call_once(|| unsafe { GDALAllRegister() });
}

fn last_error() -> String {
    let message = unsafe { CStr::from_ptr(CPLGetLastErrorMsg()) };
    message.to_string_lossy().trim().to_string()
}

/// One band's default-domain metadata plus its description, the two things
/// `gdalinfo -json` reports that record matching reads.
#[derive(Debug, Clone, Default)]
pub struct BandInfo {
    pub number: usize,
    pub description: String,
    pub metadata: HashMap<String, String>,
    /// The band's unit string, what `gdalinfo -json` reports as `unit`.
    pub unit: String,
    pub scale: f64,
    pub offset: f64,
    pub nodata: Option<f64>,
}

impl BandInfo {
    pub fn item(&self, key: &str) -> &str {
        self.metadata.get(key).map_or("", String::as_str)
    }
}

/// An open GDAL dataset. Not `Sync`: GDAL dataset handles must not be shared
/// between threads, so each worker opens its own.
pub struct Dataset {
    handle: GDALDatasetH,
    width: usize,
    height: usize,
}

impl Dataset {
    /// Open a dataset read-only. `name` is a GDAL connection string, not
    /// necessarily a filesystem path — the observation path passes
    /// `NETCDF:"file.nc":cref`.
    pub fn open(name: &Path) -> Result<Self> {
        register();
        let text = name.to_string_lossy().into_owned();
        let c_name = CString::new(text.clone()).map_err(|_| {
            EncodeError::conversion(format!("dataset name contains a NUL byte: {text}"))
        })?;
        let handle = unsafe { GDALOpen(c_name.as_ptr(), GDALAccess::GA_ReadOnly) };
        if handle.is_null() {
            return Err(EncodeError::conversion(format!(
                "cannot open {text}: {}",
                last_error()
            )));
        }
        let width = unsafe { GDALGetRasterXSize(handle) };
        let height = unsafe { GDALGetRasterYSize(handle) };
        if width <= 0 || height <= 0 {
            unsafe { GDALClose(handle) };
            return Err(EncodeError::conversion(format!("{text} has an empty grid")));
        }
        Ok(Self {
            handle,
            width: width as usize,
            height: height as usize,
        })
    }

    pub fn size(&self) -> (usize, usize) {
        (self.width, self.height)
    }

    pub fn band_count(&self) -> usize {
        (unsafe { GDALGetRasterCount(self.handle) }).max(0) as usize
    }

    pub fn geo_transform(&self) -> Result<[f64; 6]> {
        let mut transform = [0f64; 6];
        let status = unsafe { GDALGetGeoTransform(self.handle, transform.as_mut_ptr()) };
        if status != CPLErr::CE_None {
            return Err(EncodeError::conversion(format!(
                "dataset has no geotransform: {}",
                last_error()
            )));
        }
        Ok(transform)
    }

    /// The dataset's coordinate system as WKT2, the text `gdalinfo -json`
    /// prints under `coordinateSystem.wkt` (the same export options:
    /// `FORMAT=WKT2`, multi-line); empty when the dataset declares none.
    /// A projected source (HRRR's Lambert conformal grid) is recognised
    /// from it (`reproject::lambert_conformal_from_wkt`).
    pub fn projection_wkt(&self) -> String {
        let reference = unsafe { GDALGetSpatialRef(self.handle) };
        if reference.is_null() {
            return String::new();
        }
        let format = CString::new("FORMAT=WKT2").expect("static option");
        let multiline = CString::new("MULTILINE=YES").expect("static option");
        let options = [format.as_ptr(), multiline.as_ptr(), ptr::null()];
        let mut text: *mut std::ffi::c_char = ptr::null_mut();
        let status = unsafe { OSRExportToWktEx(reference, &mut text, options.as_ptr()) };
        if status != gdal_sys::OGRErr::OGRERR_NONE || text.is_null() {
            return String::new();
        }
        let wkt = unsafe { CStr::from_ptr(text) }.to_string_lossy().into_owned();
        // The string is CPLMalloc'd; VSIFree is CPLFree by another name.
        unsafe { VSIFree(text.cast()) };
        wkt
    }

    /// Dataset-level metadata of one domain (`""` is the default domain).
    pub fn metadata(&self, domain: &str) -> HashMap<String, String> {
        let domain = CString::new(domain).unwrap_or_default();
        let list = unsafe { GDALGetMetadata(self.handle.cast(), domain.as_ptr()) };
        string_list(list)
    }

    fn band(&self, number: usize) -> Result<gdal_sys::GDALRasterBandH> {
        let handle = unsafe { GDALGetRasterBand(self.handle, number as i32) };
        if handle.is_null() {
            return Err(EncodeError::conversion(format!(
                "band {number} does not exist"
            )));
        }
        Ok(handle)
    }

    pub fn band_info(&self, number: usize) -> Result<BandInfo> {
        let band = self.band(number)?;
        let empty = CString::new("").expect("empty string");
        let metadata = string_list(unsafe { GDALGetMetadata(band.cast(), empty.as_ptr()) });
        let description = unsafe { CStr::from_ptr(GDALGetDescription(band.cast())) }
            .to_string_lossy()
            .into_owned();
        let unit = unsafe { CStr::from_ptr(GDALGetRasterUnitType(band)) }
            .to_string_lossy()
            .into_owned();
        let mut has_scale = 0;
        let scale = unsafe { GDALGetRasterScale(band, &mut has_scale) };
        let mut has_offset = 0;
        let offset = unsafe { GDALGetRasterOffset(band, &mut has_offset) };
        let mut has_nodata = 0;
        let nodata = unsafe { GDALGetRasterNoDataValue(band, &mut has_nodata) };
        Ok(BandInfo {
            number,
            description,
            metadata,
            unit,
            scale: if has_scale != 0 { scale } else { 1.0 },
            offset: if has_offset != 0 { offset } else { 0.0 },
            nodata: (has_nodata != 0).then_some(nodata),
        })
    }

    pub fn bands(&self) -> Result<Vec<BandInfo>> {
        (1..=self.band_count()).map(|n| self.band_info(n)).collect()
    }

    /// Read one whole band as float64, in the dataset's own layout — the same
    /// bytes `gdal_translate -of ENVI -ot Float64` would have written.
    pub fn read_band_f64(&self, number: usize) -> Result<Vec<f64>> {
        let band = self.band(number)?;
        let mut values = vec![0f64; self.width * self.height];
        let status = unsafe {
            GDALRasterIO(
                band,
                GDALRWFlag::GF_Read,
                0,
                0,
                self.width as i32,
                self.height as i32,
                values.as_mut_ptr().cast(),
                self.width as i32,
                self.height as i32,
                GDALDataType::GDT_Float64,
                0,
                0,
            )
        };
        if status != CPLErr::CE_None {
            return Err(EncodeError::conversion(format!(
                "cannot read band {number}: {}",
                last_error()
            )));
        }
        Ok(values)
    }
}

impl Drop for Dataset {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe { GDALClose(self.handle) };
            self.handle = ptr::null_mut();
        }
    }
}

// A Dataset owns its handle exclusively and is never shared between threads by
// this crate; moving one to the thread that will read it is what the extract
// pool does.
unsafe impl Send for Dataset {}

/// The subset of `gdalinfo -json` that `xuebuild` actually reads, from the
/// linked GDAL rather than the CLI.
///
/// `xuebuild/gdal.py` uses this to inspect GRIB and netCDF inputs without a
/// system `gdalinfo` on PATH — a scheduled build converting through the
/// native encoder then needs no system GDAL at all. Only the keys the Python
/// side consumes are emitted; this is deliberately not a general
/// reimplementation of `gdalinfo`, and the fields are pinned by
/// `tests/test_gdalinfo.py`, which diffs them against the real CLI.
///
/// A dataset with no geotransform simply omits the key, the way `gdalinfo`
/// does; the caller decides whether that is fatal.
pub fn info_json(name: &Path) -> Result<serde_json::Value> {
    let _serial = needs_serial_access(name).then(netcdf_guard);
    let dataset = Dataset::open(name)?;
    let (width, height) = dataset.size();

    let mut info = serde_json::Map::new();
    info.insert("size".into(), serde_json::json!([width, height]));
    if let Ok(transform) = dataset.geo_transform() {
        info.insert("geoTransform".into(), serde_json::json!(transform));
    }
    let wkt = dataset.projection_wkt();
    if !wkt.is_empty() {
        info.insert("coordinateSystem".into(), serde_json::json!({ "wkt": wkt }));
    }
    info.insert(
        "metadata".into(),
        serde_json::json!({ "": metadata_object(&dataset.metadata("")) }),
    );

    let mut bands = Vec::with_capacity(dataset.band_count());
    for band in dataset.bands()? {
        let mut entry = serde_json::Map::new();
        entry.insert("band".into(), serde_json::json!(band.number));
        entry.insert("description".into(), serde_json::json!(band.description));
        // gdalinfo omits unit when the band declares none, scale and offset
        // at their identity values, and noDataValue when there is no fill.
        // The Python readers default all four, so matching the omissions
        // keeps the two sources indistinguishable rather than merely
        // equivalent — which is what tests/test_gdalinfo.py asserts.
        if !band.unit.is_empty() {
            entry.insert("unit".into(), serde_json::json!(band.unit));
        }
        if band.scale != 1.0 {
            entry.insert("scale".into(), serde_json::json!(band.scale));
        }
        if band.offset != 0.0 {
            entry.insert("offset".into(), serde_json::json!(band.offset));
        }
        if let Some(nodata) = band.nodata {
            entry.insert("noDataValue".into(), serde_json::json!(nodata));
        }
        entry.insert(
            "metadata".into(),
            serde_json::json!({ "": metadata_object(&band.metadata) }),
        );
        bands.push(serde_json::Value::Object(entry));
    }
    info.insert("bands".into(), serde_json::Value::Array(bands));
    Ok(serde_json::Value::Object(info))
}

/// A metadata domain as a JSON object with its keys in GDAL's own order.
///
/// `HashMap` iteration order is arbitrary, and the metadata block ends up in
/// a report a human reads and a test diffs, so it is sorted rather than left
/// to vary run to run.
fn metadata_object(items: &HashMap<String, String>) -> serde_json::Value {
    let mut keys: Vec<&String> = items.keys().collect();
    keys.sort();
    let mut object = serde_json::Map::with_capacity(keys.len());
    for key in keys {
        object.insert(key.clone(), serde_json::json!(items[key]));
    }
    serde_json::Value::Object(object)
}

fn string_list(list: *mut *mut std::ffi::c_char) -> HashMap<String, String> {
    let mut items = HashMap::new();
    if list.is_null() {
        return items;
    }
    let mut index = 0isize;
    loop {
        let entry = unsafe { *list.offset(index) };
        if entry.is_null() {
            break;
        }
        let text = unsafe { CStr::from_ptr(entry) }.to_string_lossy();
        if let Some((key, value)) = text.split_once('=') {
            items.insert(key.to_string(), value.to_string());
        }
        index += 1;
    }
    items
}
