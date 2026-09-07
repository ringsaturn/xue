//! `xue-encode` — the experimental native encoder's CLI.
//!
//! Deliberately a subset of `python -m xue`: one `convert-bin` command with the
//! same flags, so a build can be run both ways and the outputs diffed.

use std::path::PathBuf;
use std::process::ExitCode;

use xue_encode::convert::{convert_bin, ConvertOptions};
use xue_encode::errors::EncodeError;

const USAGE: &str = "\
usage: xue-encode convert-bin <input>... --output <dir> [options]

  <input>                  a GRIB directory, one or more GRIB files, or one
                           NetCDF file for an observation source
  --output <dir>           output directory for the per-variable .xue files
  --model <name>           gfs | ecmwf | sflux | radar        (default gfs)
  --profile <name>         quality | compact | balanced       (default quality)
  --manifest <path>        write a schema v5 manifest.json here
  --latest <path>          also write the mutable live pointer here
  --run <id>               the YYYYMMDDHH cycle id the pointer names
  --hours <n>              observation sources: last hour of the series
  --expected-hours <n>     the axis a --require-complete build must match
  --require-complete       reject anything but a full production run
  --bbox <w,s,e,n>         crop every plane to this window, in degrees
  --bundles <a,b>          build only these bundles
  --skip-variants          do not build the half-resolution .half.xue tiers
  --zstd-level <n>         zstd compression level               (default 15)
  --extract-workers <n>    files extracted in parallel
  --compress-workers <n>   planes compressed in parallel
  --force                  replace an existing manifest
  -v, --verbose            show progress
  -h, --help               show this message

The optional H.264 companion artifacts are not built here; they stay with the
Python pipeline and the frontend treats them as optional by design.
";

fn main() -> ExitCode {
    match run() {
        Ok(report) => {
            println!("{report}");
            ExitCode::SUCCESS
        }
        Err(Error::Usage(message)) => {
            eprintln!("{message}");
            eprint!("{USAGE}");
            ExitCode::from(2)
        }
        Err(Error::Encode(error)) => {
            eprintln!("error: {error}");
            ExitCode::from(2)
        }
    }
}

enum Error {
    Usage(String),
    Encode(EncodeError),
}

impl From<EncodeError> for Error {
    fn from(error: EncodeError) -> Self {
        Self::Encode(error)
    }
}

fn run() -> Result<String, Error> {
    let mut arguments = std::env::args().skip(1).peekable();
    let command = arguments.next().unwrap_or_default();
    if command == "-h" || command == "--help" || command.is_empty() {
        print!("{USAGE}");
        return Ok(String::new());
    }
    if command != "convert-bin" {
        return Err(Error::Usage(format!("unknown command: {command}")));
    }

    let mut inputs: Vec<PathBuf> = Vec::new();
    let mut output: Option<PathBuf> = None;
    let mut options = ConvertOptions::default();
    while let Some(argument) = arguments.next() {
        let mut value = || {
            arguments
                .next()
                .ok_or_else(|| Error::Usage(format!("{argument} needs a value")))
        };
        match argument.as_str() {
            "--output" => output = Some(PathBuf::from(value()?)),
            "--model" => options.model = value()?,
            "--profile" => options.profile = value()?,
            "--manifest" => options.manifest_path = Some(PathBuf::from(value()?)),
            "--latest" => options.latest_path = Some(PathBuf::from(value()?)),
            "--run" => options.run_id = Some(value()?),
            "--hours" => options.last_hour = Some(parse_number(&value()?)?),
            "--expected-hours" => options.expected_hours = parse_number(&value()?)?,
            "--require-complete" => options.require_complete = true,
            "--bbox" => options.bbox = Some(parse_bbox(&value()?)?),
            "--bundles" => {
                options.bundle_ids = Some(value()?.split(',').map(str::to_string).collect());
            }
            "--skip-variants" => options.skip_variants = true,
            "--skip-video" => { /* accepted for CLI parity; no video is built */ }
            "--zstd-level" => options.zstd_level = parse_number(&value()?)? as i32,
            "--extract-workers" => options.extract_workers = parse_number(&value()?)? as usize,
            "--compress-workers" => options.compress_workers = parse_number(&value()?)? as usize,
            "--force" => options.force = true,
            "-v" | "--verbose" => options.verbose = true,
            "-h" | "--help" => {
                print!("{USAGE}");
                return Ok(String::new());
            }
            other if other.starts_with('-') => {
                return Err(Error::Usage(format!("unknown option: {other}")))
            }
            other => inputs.push(PathBuf::from(other)),
        }
    }

    if inputs.is_empty() {
        return Err(Error::Usage("convert-bin needs an input".into()));
    }
    let Some(output) = output else {
        return Err(Error::Usage("convert-bin needs --output".into()));
    };
    let report = convert_bin(&inputs, &output, &options)?;
    Ok(serde_json::to_string_pretty(&report).expect("serializable report"))
}

fn parse_number(text: &str) -> Result<i64, Error> {
    text.parse()
        .map_err(|_| Error::Usage(format!("expected a number, got {text}")))
}

fn parse_bbox(text: &str) -> Result<(f64, f64, f64, f64), Error> {
    let parts: Vec<f64> = text
        .split(',')
        .map(|part| part.trim().parse::<f64>())
        .collect::<Result<_, _>>()
        .map_err(|_| Error::Usage(format!("expected four numbers in --bbox, got {text}")))?;
    match parts[..] {
        [west, south, east, north] => Ok((west, south, east, north)),
        _ => Err(Error::Usage(format!(
            "expected four numbers in --bbox, got {text}"
        ))),
    }
}
