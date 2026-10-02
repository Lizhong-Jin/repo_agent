//! Descriptor-relative primitives for trusted file tools and macOS preflight.
pub(crate) mod metadata;

use crate::directory_batch::Batch;
use crate::path_nodes::{NodeId, Paths};
use crate::scan_diagnostics::Diagnostics;
use crate::{Error, Result};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use std::collections::VecDeque;
use std::ffi::{CStr, CString, OsString};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
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
    open_cstr(fd, &name, path)
}
fn open_cstr(fd: i32, name: &CStr, path: Option<&Path>) -> Result<OwnedFd> {
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
fn validate_component(part: &[u8]) -> Result<()> {
    if part.is_empty() || part == b"." || part == b".." || part.contains(&b'/') || part.contains(&0)
    {
        return Err(Error::value("Expected a single directory entry name", None));
    }
    Ok(())
}

// Per-scan handles only; bounded independently of tree width and depth.
struct DirectoryCache {
    root: OwnedFd,
    handles: VecDeque<(NodeId, OwnedFd)>,
    scratch: Vec<NodeId>,
}
impl DirectoryCache {
    fn open(
        &mut self,
        paths: &mut Paths,
        node: NodeId,
        check: &mut Check,
        stats: &mut Diagnostics,
    ) -> Result<OwnedFd> {
        paths.chain(node, &mut self.scratch);
        let nearest = self.scratch.iter().enumerate().find_map(|(depth, id)| {
            self.handles
                .iter()
                .position(|(key, _)| key == id)
                .map(|index| (depth, index))
        });
        let (count, mut fd) = if let Some((depth, index)) = nearest {
            let (key, handle) = self.handles.remove(index).unwrap();
            let fd = duplicate(handle.as_raw_fd())?;
            self.handles.push_back((key, handle));
            (depth, fd)
        } else {
            (self.scratch.len(), duplicate(self.root.as_raw_fd())?)
        };
        stats.directory_handles_peak = stats.directory_handles_peak.max(self.handles.len() + 2);
        for id in self.scratch[..count].iter().rev() {
            check.run(false)?;
            // root + cache + old active + newly opened child coexist briefly.
            stats.directory_handles_peak = stats.directory_handles_peak.max(self.handles.len() + 3);
            fd = open_cstr(fd.as_raw_fd(), paths.name(*id), None)?;
            let cached = duplicate(fd.as_raw_fd())?;
            paths.retain(*id);
            self.handles.push_back((*id, cached));
            if self.handles.len() > 32 {
                if let Some((expired, _)) = self.handles.pop_front() {
                    paths.release(expired);
                }
            }
        }
        Ok(fd)
    }
    fn clear(&mut self, paths: &mut Paths) {
        while let Some((id, _)) = self.handles.pop_front() {
            paths.release(id);
        }
    }
}

fn descend(fd: i32, parts: &[Vec<u8>]) -> Result<OwnedFd> {
    let mut owned = duplicate(fd)?;
    for part in parts {
        validate_component(part)?;
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
fn directory_stream(fd: i32) -> Result<Directory> {
    // dup shares directory offsets. Reopen '.' for each independent enumeration.
    let owned = open_at(fd, b".", None)?;
    let raw = unsafe { libc::fdopendir(owned.as_raw_fd()) };
    if raw.is_null() {
        return Err(Error::io(std::io::Error::last_os_error(), None));
    }
    let _ = owned.into_raw_fd();
    Ok(Directory(raw))
}
fn names(fd: i32, check: &mut Check) -> Result<Vec<Vec<u8>>> {
    let directory = directory_stream(fd)?;
    let mut result = Vec::new();
    let mut batch = Batch::default();
    loop {
        let done = unsafe { batch.read(directory.0, None, || check.run(false))? };
        for entry in &batch.entries {
            result.push(batch.name(*entry).to_bytes().to_vec());
        }
        if done {
            break;
        }
    }
    Ok(result)
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
fn workspace_scan(root: Vec<u8>, cancellation: Option<Py<PyAny>>) -> Result<Diagnostics> {
    let root_path = PathBuf::from(OsString::from_vec(root.clone()));
    let root_fd = open_at(libc::AT_FDCWD, &root, Some(&root_path))?;
    let mut cache = DirectoryCache {
        root: root_fd,
        handles: VecDeque::new(),
        scratch: Vec::new(),
    };
    let mut paths = Paths::default();
    let mut pending = vec![paths.root()];
    let mut stats = Diagnostics {
        pending_tasks_peak: 1,
        ..Diagnostics::default()
    };
    let mut check = Check::new(cancellation)?;
    let (mut path, mut scratch, mut batch) = (PathBuf::new(), Vec::new(), Batch::default());
    while let Some(node) = pending.pop() {
        check.run(false)?;
        let directory = cache
            .open(&mut paths, node, &mut check, &mut stats)
            .map_err(|error| {
                paths.write_path(node, &root_path, &mut path, &mut scratch);
                error.at_path(&path)
            })?;
        let stream = directory_stream(directory.as_raw_fd()).map_err(|error| {
            paths.write_path(node, &root_path, &mut path, &mut scratch);
            error.at_path(&path)
        })?;
        stats.directory_handles_peak = stats.directory_handles_peak.max(cache.handles.len() + 3);
        loop {
            let done = unsafe { batch.read(stream.0, None, || check.run(false))? };
            stats.observe_batch(&batch);
            for entry in &batch.entries {
                check.run(false)?;
                let name = batch.name(*entry);
                let info =
                    metadata::stat_cstr(directory.as_raw_fd(), name, None).map_err(|error| {
                        paths.write_path(node, &root_path, &mut path, &mut scratch);
                        path.push(std::ffi::OsStr::from_bytes(name.to_bytes()));
                        error.at_path(&path)
                    })?;
                let kind = info.st_mode & libc::S_IFMT;
                if kind == libc::S_IFREG && info.st_nlink > 1 {
                    paths.write_path(node, &root_path, &mut path, &mut scratch);
                    path.push(std::ffi::OsStr::from_bytes(name.to_bytes()));
                    return Err(Error::value(
                        "Native 工作区含硬链接，拒绝执行：",
                        Some(&path),
                    ));
                }
                if kind == libc::S_IFDIR {
                    pending.push(paths.child(node, name.to_bytes()));
                    stats.pending_tasks_peak = stats.pending_tasks_peak.max(pending.len());
                }
            }
            if done {
                break;
            }
        }
        paths.release(node);
    }
    cache.clear(&mut paths);
    stats.observe_paths(&paths);
    check.run(true)?;
    Ok(stats)
}
struct WorkspaceJob {
    node: NodeId,
    directory: OwnedFd,
    path: PathBuf,
}
struct WorkspaceOutput {
    node: NodeId,
    children: Vec<Vec<u8>>,
    stats: Diagnostics,
}
fn workspace_directory(
    batch: &mut Batch,
    job: WorkspaceJob,
    stop: &std::sync::atomic::AtomicBool,
) -> Result<WorkspaceOutput> {
    workspace_read(batch, job, || crate::scan_pool::checkpoint(stop))
}
fn workspace_read(
    batch: &mut Batch,
    job: WorkspaceJob,
    mut check: impl FnMut() -> Result<()>,
) -> Result<WorkspaceOutput> {
    let stream =
        directory_stream(job.directory.as_raw_fd()).map_err(|error| error.at_path(&job.path))?;
    let mut output = WorkspaceOutput {
        node: job.node,
        children: Vec::new(),
        stats: Diagnostics::default(),
    };
    loop {
        let done = unsafe { batch.read(stream.0, Some(&job.path), &mut check)? };
        output.stats.observe_batch(batch);
        for entry in &batch.entries {
            check()?;
            let name = batch.name(*entry);
            let info =
                metadata::stat_cstr(job.directory.as_raw_fd(), name, None).map_err(|error| {
                    error.at_path(&job.path.join(std::ffi::OsStr::from_bytes(name.to_bytes())))
                })?;
            let kind = info.st_mode & libc::S_IFMT;
            if kind == libc::S_IFREG && info.st_nlink > 1 {
                return Err(Error::value(
                    "Native 工作区含硬链接，拒绝执行：",
                    Some(&job.path.join(std::ffi::OsStr::from_bytes(name.to_bytes()))),
                ));
            }
            if kind == libc::S_IFDIR {
                output.children.push(name.to_bytes().to_vec());
            }
        }
        if done {
            break;
        }
    }
    Ok(output)
}
fn workspace_parallel(
    root: Vec<u8>,
    cancellation: Option<Py<PyAny>>,
    workers: usize,
) -> Result<Diagnostics> {
    let mut check = Check::new(cancellation)?;
    let root_path = PathBuf::from(OsString::from_vec(root.clone()));
    let mut cache = DirectoryCache {
        root: open_at(libc::AT_FDCWD, &root, Some(&root_path))?,
        handles: VecDeque::new(),
        scratch: Vec::new(),
    };
    let mut paths = Paths::default();
    let mut pending = vec![paths.root()];
    let mut stats = Diagnostics {
        workers,
        pending_tasks_peak: 1,
        ..Diagnostics::default()
    };
    let mut pool: Option<crate::scan_pool::Pool<WorkspaceJob, WorkspaceOutput>> = None;
    let mut serial_batch = Batch::default();
    let (mut path, mut scratch) = (PathBuf::new(), Vec::new());
    while !pending.is_empty() || pool.as_ref().is_some_and(|p| p.outstanding != 0) {
        if pool.is_none() && pending.len() >= 2 {
            pool = Some(crate::scan_pool::Pool::new(
                workers,
                |_| Batch::default(),
                workspace_directory,
            )?);
        }
        let mut ready = None;
        while !pending.is_empty() && pool.as_ref().is_none_or(|p| p.outstanding < workers) {
            check.run(false)?;
            let node = pending.pop().unwrap();
            let directory = cache
                .open(&mut paths, node, &mut check, &mut stats)
                .map_err(|error| {
                    paths.write_path(node, &root_path, &mut path, &mut scratch);
                    error.at_path(&path)
                })?;
            paths.write_path(node, &root_path, &mut path, &mut scratch);
            stats.path_materialized_bytes += path.as_os_str().len();
            // Conservative bound: root/cache + two fds for each in-flight job,
            // including transient opens in the coordinator. Not per-process fd usage.
            stats.directory_handles_peak = stats
                .directory_handles_peak
                .max(cache.handles.len() + 3 + 2 * pool.as_ref().map_or(0, |p| p.outstanding));
            let job = WorkspaceJob {
                node,
                directory,
                path: path.clone(),
            };
            if let Some(pool) = pool.as_mut() {
                pool.submit(job)?;
                stats.in_flight_peak = stats.in_flight_peak.max(pool.outstanding);
            } else {
                stats.in_flight_peak = stats.in_flight_peak.max(1);
                ready = Some(workspace_read(&mut serial_batch, job, || check.run(false))?);
                break;
            }
        }
        let output = match ready {
            Some(output) => output,
            None => pool.as_mut().unwrap().receive(|| check.run(false))?,
        };
        stats.enumeration_entries_peak = stats
            .enumeration_entries_peak
            .max(output.stats.enumeration_entries_peak);
        stats.enumeration_buffer_bytes_peak = stats
            .enumeration_buffer_bytes_peak
            .max(output.stats.enumeration_buffer_bytes_peak);
        for name in output.children {
            pending.push(paths.child(output.node, &name));
        }
        stats.pending_tasks_peak = stats.pending_tasks_peak.max(pending.len());
        paths.release(output.node);
    }
    drop(pool);
    cache.clear(&mut paths);
    stats.observe_paths(&paths);
    check.run(true)?;
    Ok(stats)
}
fn workspace_dispatch(root: Vec<u8>, cancellation: Option<Py<PyAny>>) -> Result<Diagnostics> {
    let workers = crate::scan_pool::workers()?;
    if workers == 1 {
        let mut stats = workspace_scan(root, cancellation)?;
        stats.workers = 1;
        stats.in_flight_peak = 1;
        Ok(stats)
    } else {
        workspace_parallel(root, cancellation, workers)
    }
}
#[pyfunction]
#[pyo3(signature = (root, cancellation=None))]
fn check_workspace(py: Python<'_>, root: Vec<u8>, cancellation: Option<Py<PyAny>>) -> PyResult<()> {
    finish(py, py.detach(|| workspace_dispatch(root, cancellation)))?;
    Ok(())
}
#[pyfunction]
#[pyo3(signature = (root, cancellation=None))]
fn profile_workspace(
    py: Python<'_>,
    root: Vec<u8>,
    cancellation: Option<Py<PyAny>>,
) -> PyResult<Py<pyo3::types::PyDict>> {
    finish(py, py.detach(|| workspace_dispatch(root, cancellation)))?.to_python(py)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FILESYSTEM_API_VERSION", 2)?;
    m.add_function(wrap_pyfunction!(metadata::stat_many, m)?)?;
    m.add_function(wrap_pyfunction!(metadata::scan_metadata, m)?)?;
    m.add_class::<metadata::ScanMetadata>()?;
    m.add_function(wrap_pyfunction!(metadata::profile_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(open_directory_at, m)?)?;
    m.add_function(wrap_pyfunction!(list_directory, m)?)?;
    m.add_function(wrap_pyfunction!(check_workspace, m)?)?;
    m.add_function(wrap_pyfunction!(profile_workspace, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn scan_cache_is_bounded_and_uses_pinned_ancestors() {
        let root = std::env::temp_dir().join(format!(
            "directory-cache-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ));
        struct Tree(PathBuf);
        impl Drop for Tree {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        std::fs::create_dir(&root).unwrap();
        let tree = Tree(root);
        let root_fd = open_at(libc::AT_FDCWD, tree.0.as_os_str().as_bytes(), None).unwrap();
        let mut cache = DirectoryCache {
            root: root_fd,
            handles: VecDeque::new(),
            scratch: Vec::new(),
        };
        let mut path = tree.0.clone();
        let mut paths = Paths::default();
        let mut node = paths.root();
        let mut stats = Diagnostics::default();
        let mut check = Check::new(None).unwrap();
        for _ in 0..80 {
            path.push("d");
            std::fs::create_dir(&path).unwrap();
            let child = paths.child(node, b"d");
            paths.release(node);
            node = child;
            cache
                .open(&mut paths, node, &mut check, &mut stats)
                .unwrap();
            assert!(cache.handles.len() <= 32);
        }
        // Both the cached leaf and its next child are opened relative to the
        // pinned directory, even after the lexical ancestor disappears.
        std::fs::rename(tree.0.join("d"), tree.0.join("moved")).unwrap();
        cache
            .open(&mut paths, node, &mut check, &mut stats)
            .unwrap();
        let moved = tree
            .0
            .join("moved")
            .join((0..79).map(|_| "d").collect::<PathBuf>());
        std::fs::create_dir(moved.join("child")).unwrap();
        let child = paths.child(node, b"child");
        paths.release(node);
        node = child;
        cache
            .open(&mut paths, node, &mut check, &mut stats)
            .unwrap();
    }
}
