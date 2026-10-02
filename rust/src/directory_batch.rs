//! Reusable packed directory blocks, preserving readdir order and raw name bytes.
use crate::{Error, Result};
use std::ffi::CStr;
use std::path::Path;

pub(crate) const ENTRY_BATCH: usize = 256;
#[derive(Clone, Copy)]
pub(crate) struct Entry {
    start: usize,
    end: usize,
    pub dtype: u8,
}
#[derive(Default)]
pub(crate) struct Batch {
    bytes: Vec<u8>,
    pub entries: Vec<Entry>,
}
impl Batch {
    pub fn name(&self, entry: Entry) -> &CStr {
        CStr::from_bytes_with_nul(&self.bytes[entry.start..entry.end]).unwrap()
    }
    pub fn capacity_bytes(&self) -> usize {
        self.bytes.capacity() + self.entries.capacity() * std::mem::size_of::<Entry>()
    }
    // SAFETY: caller keeps DIR alive and exclusively borrowed until this returns.
    pub unsafe fn read(
        &mut self,
        dir: *mut libc::DIR,
        path: Option<&Path>,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<bool> {
        self.bytes.clear();
        self.entries.clear();
        while self.entries.len() < ENTRY_BATCH {
            checkpoint()?;
            *errno_location() = 0;
            let raw = libc::readdir(dir);
            if raw.is_null() {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(0) {
                    return Err(Error::io(error, path));
                }
                return Ok(true);
            }
            let item = &*raw;
            let name = CStr::from_ptr(item.d_name.as_ptr());
            if name.to_bytes() == b"." || name.to_bytes() == b".." {
                continue;
            }
            let start = self.bytes.len();
            self.bytes.extend_from_slice(name.to_bytes_with_nul());
            self.entries.push(Entry {
                start,
                end: self.bytes.len(),
                dtype: item.d_type,
            });
        }
        Ok(false)
    }
}
#[cfg(target_os = "linux")]
unsafe fn errno_location() -> *mut libc::c_int {
    libc::__errno_location()
}
#[cfg(target_os = "macos")]
unsafe fn errno_location() -> *mut libc::c_int {
    libc::__error()
}
