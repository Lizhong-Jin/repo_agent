//! Optional batch scanner. Python remains responsible for backend composition.
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("The native extension supports Linux and macOS only");

mod engine;
#[path = "../../filesystem/mod.rs"]
mod filesystem;
mod fs;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::Instant;

type Result<T> = std::result::Result<T, Error>;
#[derive(Debug)]
enum Error {
    Io(i32, Option<PathBuf>),
    Value(String, Option<PathBuf>),
    Python(PyErr),
}
impl Error {
    fn io(error: std::io::Error, path: Option<&Path>) -> Self {
        Self::Io(
            error.raw_os_error().unwrap_or(libc::EIO),
            path.map(Path::to_path_buf),
        )
    }
    fn value(message: &str, path: Option<&Path>) -> Self {
        Self::Value(message.into(), path.map(Path::to_path_buf))
    }
    fn into_pyerr(self, py: Python<'_>) -> PyResult<PyErr> {
        Ok(match self {
            Self::Io(number, path) => {
                let message: String = py
                    .import("os")?
                    .call_method1("strerror", (number,))?
                    .extract()?;
                // OSError chooses its errno-specific subclass. Pass a Python str
                // filename decoded with surrogateescape, as pathlib does.
                match path {
                    Some(path) => PyOSError::new_err((number, message, path.into_os_string())),
                    None => PyOSError::new_err((number, message)),
                }
            }
            Self::Value(message, path) => {
                let value = pyo3::types::PyString::new(py, &message);
                if let Some(path) = path {
                    let suffix = path.into_os_string().into_pyobject(py)?;
                    PyValueError::new_err(value.call_method1("__add__", (suffix,))?.unbind())
                } else {
                    PyValueError::new_err(message)
                }
            }
            Self::Python(error) => error,
        })
    }
}
impl From<PyErr> for Error {
    fn from(value: PyErr) -> Self {
        Self::Python(value)
    }
}

// Preserve this interpreter's exact Unicode lower() tables and surrogateescape
// semantics for uncommon non-ASCII names. ASCII names never enter Python.
fn lower(bytes: &[u8]) -> Result<Vec<u8>> {
    if bytes.is_ascii() {
        return Ok(bytes.to_ascii_lowercase());
    }
    Python::attach(|py| {
        py.import("os")?
            .call_method1("fsdecode", (PyBytes::new(py, bytes),))?
            .call_method0("lower")?
            .call_method1("encode", ("utf-8", "surrogatepass"))?
            .extract()
            .map_err(Error::from)
    })
}

fn path(bytes: Vec<u8>) -> PathBuf {
    PathBuf::from(OsString::from_vec(bytes))
}
fn paths(bytes: Vec<Vec<u8>>) -> Vec<PathBuf> {
    bytes.into_iter().map(path).collect()
}
fn required<'py, T: FromPyObjectOwned<'py>>(d: &Bound<'py, PyDict>, name: &str) -> PyResult<T> {
    d.get_item(name)?
        .ok_or_else(|| PyValueError::new_err(format!("missing {name}")))?
        .extract()
        .map_err(Into::into)
}

#[pyfunction]
fn scan(py: Python<'_>, config: &Bound<'_, PyDict>) -> PyResult<Py<PyDict>> {
    let mut scanner = engine::Scanner::new(
        path(required(config, "workspace")?),
        paths(required(config, "roots")?),
        paths(required(config, "protected_paths")?),
        paths(required(config, "pruned_paths")?),
        required(config, "names")?,
        required(config, "prefixes")?,
        required(config, "suffixes")?,
        required(config, "git_read")?,
        required(config, "mount_snapshot")?,
        required(config, "mounts")?,
        required(config, "ignore_stat_errors")?,
        config
            .get_item("cancellation")?
            .filter(|v| !v.is_none())
            .map(Bound::unbind),
    );
    let start = Instant::now();
    let outcome = py.detach(|| scanner.run());
    scanner.finish(start);
    let result = PyDict::new(py);
    result.set_item("metrics", scanner.metrics(py)?)?;
    match outcome {
        Ok((masks, git_paths)) => {
            for (name, paths) in [("masks", masks), ("git_paths", git_paths)] {
                let items = PyList::empty(py);
                for path in paths {
                    items.append(PyBytes::new(py, path.as_os_str().as_bytes()))?;
                }
                result.set_item(name, items)?;
            }
            result.set_item("error", py.None())?;
        }
        Err(error) => {
            result.set_item("error", error.into_pyerr(py)?.value(py))?;
        }
    }
    Ok(result.unbind())
}

#[pymodule]
fn repo_agent_scan(m: &Bound<'_, PyModule>) -> PyResult<()> {
    filesystem::register(m)?;
    m.add("API_VERSION", 1)?;
    m.add_function(wrap_pyfunction!(scan, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn unicode_and_undecodable_bytes_use_interpreter_semantics() {
        assert_eq!(super::lower("ΣΟΣ".as_bytes()).unwrap(), "σος".as_bytes());
        assert_eq!(
            super::lower("İK".as_bytes()).unwrap(),
            "i\u{307}k".as_bytes()
        );
        assert_eq!(super::lower(b"\xff.KEY").unwrap(), b"\xed\xb3\xbf.key");
    }
}
