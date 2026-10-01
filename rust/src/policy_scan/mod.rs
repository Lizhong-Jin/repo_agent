//! Linux policy scan binding, input conversion and matching semantics.
mod directory;
mod engine;

use crate::{Error, Result};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::PathBuf;
use std::time::Instant;

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

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
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
