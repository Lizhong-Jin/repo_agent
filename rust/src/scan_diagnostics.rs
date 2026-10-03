//! Structural high-water marks, separate from the stable cross-backend policy metrics.
use crate::{directory_batch::Batch, path_nodes::Paths};
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[derive(Default)]
pub(crate) struct Diagnostics {
    pub workers: usize,
    pub batch_size: usize,
    pub batches_submitted: usize,
    pub directories_submitted: usize,
    pub batch_directories_peak: usize,
    pub in_flight_directories_peak: usize,
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
    pub fn submitted(&mut self, directories: usize, in_flight: usize, batches: usize) {
        self.batches_submitted += 1;
        self.directories_submitted += directories;
        self.batch_directories_peak = self.batch_directories_peak.max(directories);
        self.in_flight_directories_peak = self.in_flight_directories_peak.max(in_flight);
        self.in_flight_peak = self.in_flight_peak.max(batches);
    }
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
            ("batch_size", self.batch_size),
            ("batches_submitted", self.batches_submitted),
            ("directories_submitted", self.directories_submitted),
            ("batch_directories_peak", self.batch_directories_peak),
            (
                "in_flight_directories_peak",
                self.in_flight_directories_peak,
            ),
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
