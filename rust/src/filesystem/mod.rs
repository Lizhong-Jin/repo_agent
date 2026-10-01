//! Descriptor-relative primitives for trusted file tools and macOS preflight.
use crate::{Error, Result};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use std::ffi::{CStr, CString, OsString};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd};
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

fn own(fd: i32, path: Option<&Path>) -> Result<OwnedFd> {
    if fd < 0 {
        Err(Error::io(std::io::Error::last_os_error(), path))
    } else {
        // SAFETY: each successful syscall transfers a newly owned fd.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }
}
fn duplicate(fd: i32) -> Result<OwnedFd> {
    own(unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) }, None)
}
fn open_at(fd: i32, name: &[u8], path: Option<&Path>) -> Result<OwnedFd> {
    let name = CString::new(name).map_err(|_| Error::value("embedded null byte", None))?;
    own(
        unsafe {
            libc::openat(
                fd,
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        },
        path,
    )
}
fn descend(fd: i32, parts: &[Vec<u8>]) -> Result<OwnedFd> {
    let mut owned = duplicate(fd)?;
    for part in parts {
        if part.is_empty()
            || part == b"."
            || part == b".."
            || part.contains(&b'/')
            || part.contains(&0)
        {
            return Err(Error::value("Expected a single directory entry name", None));
        }
        owned = open_at(
            owned.as_raw_fd(),
            part,
            Some(&PathBuf::from(OsString::from_vec(part.clone()))),
        )?;
    }
    Ok(owned)
}

struct Check {
    cancellation: Option<Py<PyAny>>,
    last: Instant,
}
impl Check {
    fn new(cancellation: Option<Py<PyAny>>) -> Result<Self> {
        let mut check = Self {
            cancellation,
            last: Instant::now(),
        };
        check.run(true)?;
        Ok(check)
    }
    fn run(&mut self, force: bool) -> Result<()> {
        if force || self.last.elapsed() >= Duration::from_millis(10) {
            Python::attach(|py| -> PyResult<()> {
                py.check_signals()?;
                if let Some(context) = &self.cancellation {
                    context.call_method0(py, "check")?;
                }
                Ok(())
            })?;
            self.last = Instant::now();
        }
        Ok(())
    }
}
struct Directory(*mut libc::DIR);
impl Drop for Directory {
    fn drop(&mut self) {
        unsafe {
            libc::closedir(self.0);
        }
    }
}
fn names(fd: i32, check: &mut Check) -> Result<Vec<Vec<u8>>> {
    // dup alone shares the directory offset. Open '.' to give each enumeration
    // its own offset, while staying anchored to the original directory inode.
    let owned = open_at(fd, b".", None)?;
    let raw = unsafe { libc::fdopendir(owned.as_raw_fd()) };
    if raw.is_null() {
        return Err(Error::io(std::io::Error::last_os_error(), None));
    }
    let _ = owned.into_raw_fd(); // fdopendir now owns this fd.
    let directory = Directory(raw);
    let mut result = Vec::new();
    loop {
        check.run(false)?;
        unsafe {
            *errno_location() = 0;
        }
        let entry = unsafe { libc::readdir(directory.0) };
        if entry.is_null() {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(0) {
                return Err(Error::io(error, None));
            }
            break;
        }
        let bytes = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if bytes != b"." && bytes != b".." {
            result.push(bytes.to_vec());
        }
    }
    Ok(result)
}
#[cfg(target_os = "linux")]
unsafe fn errno_location() -> *mut libc::c_int {
    libc::__errno_location()
}
#[cfg(target_os = "macos")]
unsafe fn errno_location() -> *mut libc::c_int {
    libc::__error()
}

fn finish<T>(py: Python<'_>, result: Result<T>) -> PyResult<T> {
    result.map_err(|error| error.into_pyerr(py).unwrap_or_else(|error| error))
}
#[pyfunction]
fn open_directory_at(py: Python<'_>, fd: i32, parts: Vec<Vec<u8>>) -> PyResult<i32> {
    let pinned = finish(py, duplicate(fd))?;
    let owned = finish(py, py.detach(|| descend(pinned.as_raw_fd(), &parts)))?;
    Ok(owned.into_raw_fd())
}
#[pyfunction]
#[pyo3(signature = (fd, cancellation=None))]
fn list_directory(
    py: Python<'_>,
    fd: i32,
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Py<PyList>> {
    let pinned = finish(py, duplicate(fd))?;
    let result = py.detach(|| names(pinned.as_raw_fd(), &mut Check::new(cancellation)?));
    let output = PyList::empty(py);
    for name in finish(py, result)? {
        output.append(PyBytes::new(py, &name))?;
    }
    Ok(output.unbind())
}
#[pyfunction]
#[pyo3(signature = (root, cancellation=None))]
fn check_workspace(py: Python<'_>, root: Vec<u8>, cancellation: Option<Py<PyAny>>) -> PyResult<()> {
    let result = py.detach(|| -> Result<()> {
        let path = PathBuf::from(OsString::from_vec(root.clone()));
        let root_fd = open_at(libc::AT_FDCWD, &root, Some(&path))?;
        let mut pending = vec![(Vec::<Vec<u8>>::new(), path)];
        let mut check = Check::new(cancellation)?;
        // Queue paths, not open descriptors: fd use stays bounded on deep/wide trees.
        while let Some((parts, path)) = pending.pop() {
            check.run(false)?;
            let directory = descend(root_fd.as_raw_fd(), &parts)?;
            for name in names(directory.as_raw_fd(), &mut check)? {
                check.run(false)?;
                let child = path.join(OsString::from_vec(name.clone()));
                let c_name = CString::new(name.clone()).unwrap();
                let mut info = std::mem::MaybeUninit::<libc::stat>::uninit();
                let rc = unsafe {
                    libc::fstatat(
                        directory.as_raw_fd(),
                        c_name.as_ptr(),
                        info.as_mut_ptr(),
                        libc::AT_SYMLINK_NOFOLLOW,
                    )
                };
                if rc != 0 {
                    return Err(Error::io(std::io::Error::last_os_error(), Some(&child)));
                }
                let info = unsafe { info.assume_init() };
                let kind = info.st_mode & libc::S_IFMT;
                if kind == libc::S_IFREG && info.st_nlink > 1 {
                    return Err(Error::value(
                        "Native 工作区含硬链接，拒绝执行：",
                        Some(&child),
                    ));
                }
                if kind == libc::S_IFDIR {
                    let mut descendants = parts.clone();
                    descendants.push(name);
                    pending.push((descendants, child));
                }
            }
        }
        check.run(true)
    });
    finish(py, result)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FILESYSTEM_API_VERSION", 1)?;
    m.add_function(wrap_pyfunction!(open_directory_at, m)?)?;
    m.add_function(wrap_pyfunction!(list_directory, m)?)?;
    m.add_function(wrap_pyfunction!(check_workspace, m)?)?;
    Ok(())
}
