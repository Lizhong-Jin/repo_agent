//! Shared Python-compatible error conversion.
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use std::path::{Path, PathBuf};

pub(crate) type Result<T> = std::result::Result<T, Error>;
#[derive(Debug)]
pub(crate) enum Error {
    Io(i32, Option<PathBuf>),
    Value(String, Option<PathBuf>),
    Python(PyErr),
}
impl Error {
    pub(crate) fn at_path(self, path: &Path) -> Self {
        match self {
            Self::Io(code, _) => Self::Io(code, Some(path.to_path_buf())),
            other => other,
        }
    }

    pub(crate) fn io(error: std::io::Error, path: Option<&Path>) -> Self {
        Self::Io(
            error.raw_os_error().unwrap_or(libc::EIO),
            path.map(Path::to_path_buf),
        )
    }
    pub(crate) fn value(message: &str, path: Option<&Path>) -> Self {
        Self::Value(message.into(), path.map(Path::to_path_buf))
    }
    pub(crate) fn into_pyerr(self, py: Python<'_>) -> PyResult<PyErr> {
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
