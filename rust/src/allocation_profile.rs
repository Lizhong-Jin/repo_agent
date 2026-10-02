//! Opt-in allocator telemetry for isolated benchmarks, never enabled in release wheels.
use pyo3::prelude::*;
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

struct Measured;
#[global_allocator]
static ALLOCATOR: Measured = Measured;
static ALLOCS: AtomicU64 = AtomicU64::new(0);
static REALLOCS: AtomicU64 = AtomicU64::new(0);
static BYTES: AtomicU64 = AtomicU64::new(0);
static LIVE: AtomicU64 = AtomicU64::new(0);
static PEAK: AtomicU64 = AtomicU64::new(0);

fn added(size: usize) {
    BYTES.fetch_add(size as u64, Relaxed);
    let live = LIVE.fetch_add(size as u64, Relaxed) + size as u64;
    PEAK.fetch_max(live, Relaxed);
}
// SAFETY: preserve System's layout/ownership contract, never allocate in telemetry.
unsafe impl GlobalAlloc for Measured {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = System.alloc(layout);
        if !ptr.is_null() {
            ALLOCS.fetch_add(1, Relaxed);
            added(layout.size());
        }
        ptr
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = System.alloc_zeroed(layout);
        if !ptr.is_null() {
            ALLOCS.fetch_add(1, Relaxed);
            added(layout.size());
        }
        ptr
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout);
        LIVE.fetch_sub(layout.size() as u64, Relaxed);
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        let result = System.realloc(ptr, layout, size);
        if !result.is_null() {
            REALLOCS.fetch_add(1, Relaxed);
            LIVE.fetch_sub(layout.size() as u64, Relaxed);
            added(size);
        }
        result
    }
}

/// Process-wide extension allocations only; resetting peak requires a quiet process.
#[pyfunction]
#[pyo3(signature = (reset_peak=false))]
fn allocation_stats(reset_peak: bool) -> (u64, u64, u64, u64, u64) {
    if reset_peak {
        PEAK.store(LIVE.load(Relaxed), Relaxed);
    }
    (
        ALLOCS.load(Relaxed),
        REALLOCS.load(Relaxed),
        BYTES.load(Relaxed),
        LIVE.load(Relaxed),
        PEAK.load(Relaxed),
    )
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(allocation_stats, m)?)
}
