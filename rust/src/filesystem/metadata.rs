//! Batched lstat observations. No snapshot or permission to read is implied.
use super::{duplicate, finish, Check};
use crate::{Error, Result};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::ffi::{CStr, CString};
use std::os::fd::AsRawFd;
use std::path::Path;

pub(crate) fn stat_at(fd: i32, name: &[u8], path: Option<&Path>) -> Result<libc::stat> {
    let name = CString::new(name).map_err(|_| Error::value("embedded null byte", None))?;
    stat_cstr(fd, &name, path)
}

pub(crate) fn stat_cstr(fd: i32, name: &CStr, path: Option<&Path>) -> Result<libc::stat> {
    let mut info = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: fd remains pinned, name is terminated; success initializes stat.
    let rc = unsafe {
        libc::fstatat(
            fd,
            name.as_ptr(),
            info.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if rc != 0 {
        return Err(Error::io(std::io::Error::last_os_error(), path));
    }
    Ok(unsafe { info.assume_init() })
}

#[pyfunction]
#[pyo3(signature = (fd, names, cancellation=None))]
pub(super) fn stat_many(
    py: Python<'_>,
    fd: i32,
    names: Vec<Vec<u8>>,
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Py<PyList>> {
    for name in &names {
        super::validate_component(name).map_err(|e| e.into_pyerr(py).unwrap_or_else(|e| e))?;
    }
    let pinned = finish(py, duplicate(fd))?;
    let results = finish(
        py,
        py.detach(|| -> Result<_> {
            let mut check = Check::new(cancellation)?;
            let mut results = Vec::with_capacity(names.len());
            for name in names {
                check.run(false)?;
                match stat_at(pinned.as_raw_fd(), &name, None) {
                    Ok(info) => results.push(Ok(info)),
                    Err(Error::Io(errno, _)) => results.push(Err(errno)),
                    Err(error) => return Err(error),
                }
            }
            check.run(true)?;
            Ok(results)
        }),
    )?;
    let output = PyList::empty(py);
    for result in results {
        let info = match result {
            Ok(info) => info,
            Err(errno) => {
                output.append(errno)?;
                continue;
            }
        };
        let times = [
            ("st_atime", "st_atime_ns", info.st_atime, info.st_atime_nsec),
            ("st_mtime", "st_mtime_ns", info.st_mtime, info.st_mtime_nsec),
            ("st_ctime", "st_ctime_ns", info.st_ctime, info.st_ctime_nsec),
        ];
        let extra = PyDict::new(py);
        for (float_key, ns_key, seconds, nanos) in times {
            extra.set_item(float_key, seconds as f64 + nanos as f64 * 1e-9)?;
            extra.set_item(ns_key, seconds as i128 * 1_000_000_000 + nanos as i128)?;
        }
        extra.set_item("st_blksize", info.st_blksize)?;
        extra.set_item("st_blocks", info.st_blocks)?;
        extra.set_item("st_rdev", info.st_rdev)?;
        #[cfg(target_os = "macos")]
        {
            extra.set_item(
                "st_birthtime",
                info.st_birthtime as f64 + info.st_birthtime_nsec as f64 * 1e-9,
            )?;
            extra.set_item(
                "st_birthtime_ns",
                info.st_birthtime as i128 * 1_000_000_000 + info.st_birthtime_nsec as i128,
            )?;
            extra.set_item("st_flags", info.st_flags)?;
            extra.set_item("st_gen", info.st_gen)?;
        }
        output.append((
            (
                info.st_mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                info.st_uid,
                info.st_gid,
                info.st_size,
                info.st_atime,
                info.st_mtime,
                info.st_ctime,
            ),
            extra,
        ))?;
    }
    Ok(output.unbind())
}

/// Internal observations used by traversal/content guards, not a replacement for stat_result.
#[pyclass(frozen, get_all, module = "rust_backend")]
pub(super) struct ScanMetadata {
    st_mode: u32,
    st_dev: u64,
    st_ino: u64,
    st_nlink: u64,
    st_size: i64,
    st_mtime_ns: i128,
    st_ctime_ns: i128,
}
impl From<libc::stat> for ScanMetadata {
    #[allow(clippy::unnecessary_cast)] // libc widths differ between Linux and macOS.
    fn from(info: libc::stat) -> Self {
        Self {
            st_mode: info.st_mode as u32,
            st_dev: info.st_dev as u64,
            st_ino: info.st_ino as u64,
            st_nlink: info.st_nlink as u64,
            st_size: info.st_size as i64,
            st_mtime_ns: info.st_mtime as i128 * 1_000_000_000 + info.st_mtime_nsec as i128,
            st_ctime_ns: info.st_ctime as i128 * 1_000_000_000 + info.st_ctime_nsec as i128,
        }
    }
}

#[pyfunction]
#[pyo3(signature = (fd, packed_names, cancellation=None))]
pub(super) fn scan_metadata(
    py: Python<'_>,
    fd: i32,
    packed_names: &[u8],
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Py<PyList>> {
    compact_output(py, compact_results(py, fd, packed_names, cancellation)?)
}

fn compact_results(
    py: Python<'_>,
    fd: i32,
    packed_names: &[u8],
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Vec<std::result::Result<ScanMetadata, i32>>> {
    // Python bytes stays borrowed/alive across detach and is immutable. Each
    // NUL-terminated component can go directly to fstatat without a CString copy.
    for bytes in packed_names.split_inclusive(|b| *b == 0) {
        let name = CStr::from_bytes_with_nul(bytes).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err("Expected NUL-terminated names")
        })?;
        finish(py, super::validate_component(name.to_bytes()))?;
    }
    let pinned = finish(py, duplicate(fd))?;
    finish(
        py,
        py.detach(|| -> Result<_> {
            let mut check = Check::new(cancellation)?;
            let mut results = Vec::with_capacity(packed_names.iter().filter(|b| **b == 0).count());
            for bytes in packed_names.split_inclusive(|b| *b == 0) {
                check.run(false)?;
                let name = CStr::from_bytes_with_nul(bytes).unwrap();
                match stat_cstr(pinned.as_raw_fd(), name, None) {
                    Ok(info) => results.push(Ok(ScanMetadata::from(info))),
                    Err(Error::Io(code, _)) => results.push(Err(code)),
                    Err(error) => return Err(error),
                }
            }
            check.run(true)?;
            Ok(results)
        }),
    )
}

fn compact_output(
    py: Python<'_>,
    results: Vec<std::result::Result<ScanMetadata, i32>>,
) -> PyResult<Py<PyList>> {
    let output = PyList::empty(py);
    for result in results {
        match result {
            Ok(info) => output.append(Py::new(py, info)?)?,
            Err(code) => output.append(code)?,
        }
    }
    Ok(output.unbind())
}

/// Diagnostic read; timings separate native observation from Python object creation.
#[pyfunction]
#[pyo3(signature = (fd, packed_names, cancellation=None))]
pub(super) fn profile_metadata(
    py: Python<'_>,
    fd: i32,
    packed_names: &[u8],
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Py<PyDict>> {
    let started = std::time::Instant::now();
    let results = compact_results(py, fd, packed_names, cancellation)?;
    let observe_ms = started.elapsed().as_secs_f64() * 1000.0;
    let entries = results.len();
    let started = std::time::Instant::now();
    let _output = compact_output(py, results)?;
    let conversion_ms = started.elapsed().as_secs_f64() * 1000.0;
    let report = PyDict::new(py);
    report.set_item("observe_ms", observe_ms)?;
    report.set_item("python_conversion_ms", conversion_ms)?;
    report.set_item("entries", entries)?;
    Ok(report.unbind())
}
