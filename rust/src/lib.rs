//! Shared Rust backend: bindings compose independent filesystem and policy modules.
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("The native extension supports Linux and macOS only");

#[cfg(feature = "allocation-profile")]
mod allocation_profile;
mod directory_batch;
mod error;
mod filesystem;
mod path_nodes;
mod policy_scan;
mod scan_diagnostics;
mod scan_pool;

pub(crate) use error::{Error, Result};
use pyo3::prelude::*;

#[pymodule]
fn rust_backend(m: &Bound<'_, PyModule>) -> PyResult<()> {
    #[cfg(feature = "allocation-profile")]
    allocation_profile::register(m)?;
    m.add("SCAN_PARALLEL_VERSION", 1)?;
    m.add("SCAN_BATCH_VERSION", 1)?;
    policy_scan::register(m)?;
    filesystem::register(m)?;
    Ok(())
}
