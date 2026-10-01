//! Shared Rust backend: bindings compose independent filesystem and policy modules.
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("The native extension supports Linux and macOS only");

mod error;
mod filesystem;
mod policy_scan;

pub(crate) use error::{Error, Result};
use pyo3::prelude::*;

#[pymodule]
fn rust_backend(m: &Bound<'_, PyModule>) -> PyResult<()> {
    policy_scan::register(m)?;
    filesystem::register(m)?;
    Ok(())
}
