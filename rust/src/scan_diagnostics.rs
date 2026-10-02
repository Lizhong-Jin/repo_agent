//! Structural high-water marks, separate from the stable cross-backend policy metrics.
use crate::{directory_batch::Batch, path_nodes::Paths};
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[derive(Default)]
pub(crate) struct Diagnostics {
    pub workers: usize,
    pub in_flight_peak: usize,
    pub path_nodes_created: usize,
    pub path_nodes_peak: usize,
    pub path_name_copied_bytes: usize,
    pub path_materialized_bytes: usize,
    pub pending_tasks_peak: usize,
    pub directory_handles_peak: usize,
    pub enumeration_entries_peak: usize,
    pub enumeration_buffer_bytes_peak: usize,
}
impl Diagnostics {
    pub fn observe_paths(&mut self, paths: &Paths) {
        self.path_nodes_created += paths.created;
        self.path_nodes_peak = self.path_nodes_peak.max(paths.peak_live);
        self.path_name_copied_bytes += paths.copied_name_bytes;
    }
    pub fn observe_batch(&mut self, batch: &Batch) {
        self.enumeration_entries_peak = self.enumeration_entries_peak.max(batch.entries.len());
        self.enumeration_buffer_bytes_peak = self
            .enumeration_buffer_bytes_peak
            .max(batch.capacity_bytes());
    }
    pub fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let output = PyDict::new(py);
        for (name, value) in [
            ("workers", self.workers),
            ("in_flight_peak", self.in_flight_peak),
            ("path_nodes_created", self.path_nodes_created),
            ("path_nodes_peak", self.path_nodes_peak),
            ("path_name_copied_bytes", self.path_name_copied_bytes),
            ("path_materialized_bytes", self.path_materialized_bytes),
            ("pending_tasks_peak", self.pending_tasks_peak),
            ("directory_handles_peak", self.directory_handles_peak),
            ("enumeration_entries_peak", self.enumeration_entries_peak),
            (
                "enumeration_buffer_bytes_peak",
                self.enumeration_buffer_bytes_peak,
            ),
        ] {
            output.set_item(name, value)?;
        }
        Ok(output.unbind())
    }
}
