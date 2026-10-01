//! Batched lstat observations. No snapshot or permission to read is implied.
use super::{duplicate, finish, Check};
use crate::{Error, Result};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::ffi::CString;
use std::os::fd::AsRawFd;
use std::path::Path;

pub(crate) fn stat_at(fd: i32, name: &[u8], path: Option<&Path>) -> Result<libc::stat> {
    let name = CString::new(name).map_err(|_| Error::value("embedded null byte", None))?;
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
