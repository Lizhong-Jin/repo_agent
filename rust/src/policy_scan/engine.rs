use super::{directory::Directory, lower};
use crate::directory_batch::Batch;
use crate::path_nodes::{NodeId, Paths};
use crate::scan_diagnostics::Diagnostics;
use crate::scan_pool::Pool;
use crate::{Error, Result};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{HashMap, HashSet};
use std::ffi::OsString;
use std::fs;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Clone, Copy)]
#[repr(usize)]
enum Metric {
    DirectoryOpens,
    MetadataChecks,
    DirectoriesScanned,
    DirectoriesReused,
    EntriesClassified,
    Roots,
    AliasRoots,
    Masks,
    GitPaths,
    WorkspaceDirectoriesScanned,
    WorkspaceEntriesChecked,
    ScanMs,
    EnumerationMs,
    RulesMs,
    MappingMs,
    MetadataMs,
    WorkspaceValidationMs,
}
const METRICS: &[&str] = &[
    "directory_opens",
    "metadata_checks",
    "directories_scanned",
    "directories_reused",
    "entries_classified",
    "roots",
    "alias_roots",
    "masks",
    "git_paths",
    "workspace_directories_scanned",
    "workspace_entries_checked",
    "scan_ms",
    "enumeration_ms",
    "rules_ms",
    "mapping_ms",
    "metadata_ms",
    "workspace_validation_ms",
];
#[derive(Default)]
struct Values {
    values: [f64; METRICS.len()],
    present: u32,
}
impl Values {
    fn add(&mut self, key: Metric, value: f64) {
        self.set(key, self.get(key) + value);
    }
    fn get(&self, key: Metric) -> f64 {
        self.values[key as usize]
    }
    fn set(&mut self, key: Metric, value: f64) {
        self.values[key as usize] = value;
        self.present |= 1 << key as usize;
    }
    fn populate(&self, d: &Bound<'_, PyDict>) -> PyResult<()> {
        for (index, name) in METRICS.iter().enumerate() {
            if self.present & (1 << index) == 0 {
                continue;
            }
            if index < Metric::ScanMs as usize {
                d.set_item(name, self.values[index] as u64)?;
            } else {
                d.set_item(name, self.values[index])?;
            }
        }
        Ok(())
    }
}
type MountInput = (Vec<u8>, String);
struct Mount {
    path: PathBuf,
    filesystem: String,
}
struct Bucket {
    root: PathBuf,
    mount: Option<usize>,
    values: Values,
}
struct Fact {
    name: OsString,
    directory: bool,
    link: bool,
    protected: bool,
}
enum Facts {
    Entries(Vec<Fact>),
    Denied(i32),
}

struct EntryJob {
    node: NodeId,
    masked: bool,
    mount: Option<usize>,
    directory: PathBuf,
    canonical: PathBuf,
    root: PathBuf,
}
struct EntryOutput {
    job: EntryJob,
    facts: Result<Arc<Facts>>,
    values: Values,
    diagnostics: Diagnostics,
}
fn scan_directory(worker: &mut Scanner, job: EntryJob, _stop: &AtomicBool) -> Result<EntryOutput> {
    worker.active_root.clone_from(&job.root);
    let facts = worker.entries(&job.directory, &job.canonical, false);
    Ok(EntryOutput {
        job,
        facts,
        values: std::mem::take(&mut worker.values),
        diagnostics: std::mem::take(&mut worker.diagnostics),
    })
}

pub struct Scanner {
    workspace: PathBuf,
    roots: Vec<PathBuf>,
    protected_paths: Vec<PathBuf>,
    protected_children: HashMap<PathBuf, HashSet<OsString>>,
    protected_names: HashSet<OsString>,
    pruned: HashSet<PathBuf>,
    names: HashSet<Vec<u8>>,
    prefixes: Vec<Vec<u8>>,
    suffixes: Vec<Vec<u8>>,
    git_read: bool,
    snapshot: Vec<u8>,
    mounts: Vec<Mount>,
    mount_index: HashMap<PathBuf, usize>,
    ignore_stat_errors: bool,
    cache: HashMap<PathBuf, Arc<Facts>>,
    values: Values,
    complete: bool,
    buckets: Vec<Bucket>,
    bucket_index: HashMap<(PathBuf, Option<usize>), usize>,
    active_bucket: Option<usize>,
    active_root: PathBuf,
    cancellation: Option<Py<PyAny>>,
    last_check: Instant,
    diagnostics: Diagnostics,
    batch: Batch,
    lower_scratch: Vec<u8>,
    worker_stop: Option<Arc<AtomicBool>>,
    parallel_wait_ms: f64,
}

impl Scanner {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        workspace: PathBuf,
        roots: Vec<PathBuf>,
        protected_paths: Vec<PathBuf>,
        pruned: Vec<PathBuf>,
        names: Vec<Vec<u8>>,
        prefixes: Vec<Vec<u8>>,
        suffixes: Vec<Vec<u8>>,
        git_read: bool,
        snapshot: Vec<u8>,
        mounts: Vec<MountInput>,
        ignore_stat_errors: bool,
        cancellation: Option<Py<PyAny>>,
    ) -> Self {
        let mut children: HashMap<PathBuf, HashSet<OsString>> = HashMap::new();
        let mut protected_names = HashSet::new();
        for p in &protected_paths {
            if let Some(name) = p.file_name() {
                protected_names.insert(name.to_os_string());
                children
                    .entry(p.parent().unwrap_or(p).to_path_buf())
                    .or_default()
                    .insert(name.to_os_string());
            }
        }
        Self {
            workspace,
            roots,
            protected_paths,
            protected_children: children,
            protected_names,
            pruned: pruned.into_iter().collect(),
            names: names.into_iter().collect(),
            prefixes,
            suffixes,
            git_read,
            snapshot,
            mount_index: mounts
                .iter()
                .enumerate()
                .map(|(i, (p, _))| (PathBuf::from(OsString::from_vec(p.clone())), i))
                .collect(),
            mounts: mounts
                .into_iter()
                .map(|(p, filesystem)| Mount {
                    path: PathBuf::from(OsString::from_vec(p)),
                    filesystem,
                })
                .collect(),
            ignore_stat_errors,
            cache: HashMap::new(),
            values: Values {
                present: (1 << METRICS.len()) - 1,
                ..Values::default()
            },
            complete: false,
            buckets: Vec::new(),
            bucket_index: HashMap::new(),
            active_bucket: None,
            active_root: PathBuf::new(),
            cancellation,
            last_check: Instant::now(),
            diagnostics: Diagnostics::default(),
            batch: Batch::default(),
            lower_scratch: Vec::new(),
            worker_stop: None,
            parallel_wait_ms: 0.0,
        }
    }
    fn worker(&self, stop: Arc<AtomicBool>) -> Self {
        // Only immutable classification inputs are copied once per worker. Cache,
        // root identities, mount buckets and policy decisions stay in the coordinator.
        let mut worker = Self::new(
            self.workspace.clone(),
            Vec::new(),
            self.protected_paths.clone(),
            Vec::new(),
            self.names.iter().cloned().collect(),
            self.prefixes.clone(),
            self.suffixes.clone(),
            false,
            Vec::new(),
            Vec::new(),
            self.ignore_stat_errors,
            None,
        );
        worker.values = Values::default();
        worker.worker_stop = Some(stop);
        worker
    }
    fn merge_values(&mut self, values: Values) {
        for index in 0..METRICS.len() {
            if values.present & (1 << index) != 0 {
                self.values.values[index] += values.values[index];
                self.values.present |= 1 << index;
                if let Some(bucket) = self.active_bucket {
                    self.buckets[bucket].values.values[index] += values.values[index];
                    self.buckets[bucket].values.present |= 1 << index;
                }
            }
        }
    }
    fn checkpoint(&mut self, force: bool) -> Result<()> {
        if let Some(stop) = &self.worker_stop {
            return crate::scan_pool::checkpoint(stop);
        }
        if force || self.last_check.elapsed() >= Duration::from_millis(10) {
            Python::attach(|py| -> PyResult<()> {
                py.check_signals()?;
                if let Some(context) = &self.cancellation {
                    context.call_method0(py, "check")?;
                }
                Ok(())
            })?;
            self.last_check = Instant::now();
        }
        Ok(())
    }
    fn record(&mut self, key: Metric, value: f64) {
        self.values.add(key, value);
        if let Some(index) = self.active_bucket {
            self.buckets[index].values.add(key, value);
        }
    }
    fn validates(&self, directory: &Path) -> bool {
        directory.starts_with(&self.workspace)
    }
    fn protected(&mut self, name: &[u8]) -> Result<bool> {
        if name.is_ascii() {
            self.lower_scratch.clear();
            self.lower_scratch
                .extend(name.iter().map(u8::to_ascii_lowercase));
        } else {
            self.lower_scratch = lower(name)?;
        }
        let name = &self.lower_scratch;
        Ok(self.names.contains(name)
            || self.prefixes.iter().any(|v| name.starts_with(v))
            || self.suffixes.iter().any(|v| name.ends_with(v)))
    }
    fn containing(&self, path: &Path) -> Option<usize> {
        self.mounts
            .iter()
            .enumerate()
            .filter(|(_, m)| path.starts_with(&m.path))
            .max_by_key(|(_, m)| m.path.as_os_str().len())
            .map(|(i, _)| i)
    }
    fn set_bucket(&mut self, root: &Path, mount: Option<usize>) {
        let key = (root.to_path_buf(), mount);
        let index = *self.bucket_index.entry(key).or_insert_with(|| {
            let index = self.buckets.len();
            self.buckets.push(Bucket {
                root: root.to_path_buf(),
                mount,
                values: Values::default(),
            });
            index
        });
        self.active_bucket = Some(index);
    }
    fn metadata(&self, path: &Path, follow: bool) -> Result<fs::Metadata> {
        (if follow {
            fs::metadata(path)
        } else {
            fs::symlink_metadata(path)
        })
        .map_err(|e| Error::io(e, Some(path)))
    }
    fn is_type(&self, path: &Path, directory: bool) -> Result<bool> {
        match self.metadata(path, directory) {
            Ok(info) => Ok(if directory {
                info.is_dir()
            } else {
                info.is_symlink()
            }),
            Err(Error::Io(n, _))
                if self.ignore_stat_errors
                    || [libc::ENOENT, libc::ENOTDIR, libc::EBADF, libc::ELOOP].contains(&n) =>
            {
                Ok(false)
            }
            Err(e) => Err(e),
        }
    }
    fn canonical(&self, path: &Path) -> Result<PathBuf> {
        fs::canonicalize(path).map_err(|e| Error::io(e, Some(path)))
    }

    fn entries(&mut self, directory: &Path, canonical: &Path, reuse: bool) -> Result<Arc<Facts>> {
        self.checkpoint(false)?;
        if let Some(facts) = self.cache.get(canonical).filter(|_| reuse).cloned() {
            let start = Instant::now();
            if directory != self.active_root {
                let info = self.metadata(directory, false)?;
                self.record(Metric::MetadataChecks, 1.0);
                if !info.is_dir() {
                    return Err(Error::value(
                        "Linux native 扫描期间目录变为符号链接或非目录：",
                        Some(directory),
                    ));
                }
            }
            self.record(Metric::MetadataMs, elapsed(start));
            self.record(Metric::DirectoriesReused, 1.0);
            return Ok(facts);
        }
        self.record(Metric::DirectoriesScanned, 1.0);
        let result = self.enumerate(directory, canonical);
        let facts = match result {
            Err(Error::Io(n, _))
                if [libc::EACCES, libc::EPERM].contains(&n) && !self.validates(directory) =>
            {
                Facts::Denied(n)
            }
            other => other?,
        };
        let facts = Arc::new(facts);
        if reuse {
            self.cache.insert(canonical.to_path_buf(), facts.clone());
        }
        Ok(facts)
    }
    fn enumerate(&mut self, directory: &Path, canonical: &Path) -> Result<Facts> {
        let start = Instant::now();
        let mut fd = Directory::open(if directory == self.active_root {
            canonical
        } else {
            directory
        })?;
        self.record(Metric::DirectoryOpens, 1.0);
        self.diagnostics.directory_handles_peak = 1;
        self.record(Metric::MetadataMs, elapsed(start));
        // Temporarily move the reusable block so callbacks may borrow self.
        let mut batch = std::mem::take(&mut self.batch);
        let result = self.classify_blocks(&mut fd, directory, &mut batch);
        self.batch = batch;
        result
    }
    fn classify_blocks(
        &mut self,
        fd: &mut Directory,
        directory: &Path,
        batch: &mut Batch,
    ) -> Result<Facts> {
        let validates = self.validates(directory);
        if validates {
            self.record(Metric::WorkspaceDirectoriesScanned, 1.0);
        }
        let mut facts = Vec::new();
        let mut count = 0;
        let mut child_path = directory.to_path_buf();
        loop {
            let start = Instant::now();
            let done = fd.read_batch(directory, batch, || self.checkpoint(false))?;
            let mut kinds = [(false, false); crate::directory_batch::ENTRY_BATCH];
            for (index, entry) in batch.entries.iter().enumerate() {
                kinds[index] = fd.kind(batch.name(*entry), entry.dtype, directory)?;
            }
            self.record(Metric::EnumerationMs, elapsed(start));
            self.diagnostics.observe_batch(batch);
            if validates {
                let start = Instant::now();
                let checked = (|| -> Result<()> {
                    for (index, entry) in batch.entries.iter().enumerate() {
                        if index % 64 == 0 {
                            self.checkpoint(false)?;
                        }
                        if kinds[index].0 {
                            continue;
                        }
                        let name = batch.name(*entry);
                        let info = fd.metadata(name).map_err(|error| {
                            child_path.push(std::ffi::OsStr::from_bytes(name.to_bytes()));
                            error.at_path(&child_path)
                        })?;
                        let kind = info.st_mode & libc::S_IFMT;
                        if kind == libc::S_IFREG && info.st_nlink > 1 {
                            child_path.push(std::ffi::OsStr::from_bytes(name.to_bytes()));
                            return Err(Error::value(
                                "Native 工作区含硬链接，拒绝执行：",
                                Some(&child_path),
                            ));
                        }
                        if !(kind == libc::S_IFREG || kind == libc::S_IFLNK) {
                            return Err(Error::value(
                                "Linux native 工作区含 socket/FIFO/设备等特殊文件，拒绝执行",
                                None,
                            ));
                        }
                        self.record(Metric::WorkspaceEntriesChecked, 1.0);
                    }
                    Ok(())
                })();
                self.record(Metric::WorkspaceValidationMs, elapsed(start));
                checked?;
            }
            let start = Instant::now();
            for (index, entry) in batch.entries.iter().enumerate() {
                if index % 64 == 0 {
                    self.checkpoint(false)?;
                }
                let name = batch.name(*entry);
                let protected = self.protected(name.to_bytes())?;
                let name = std::ffi::OsStr::from_bytes(name.to_bytes());
                let (is_dir, symlink) = kinds[index];
                if is_dir || protected || self.protected_names.contains(name) {
                    facts.push(Fact {
                        name: name.to_os_string(),
                        directory: is_dir,
                        link: symlink,
                        protected,
                    });
                }
                count += 1;
            }
            self.record(Metric::RulesMs, elapsed(start));
            if done {
                break;
            }
        }
        self.record(Metric::EntriesClassified, count as f64);
        Ok(Facts::Entries(facts))
    }
    pub fn run(&mut self) -> Result<(Vec<PathBuf>, Vec<PathBuf>)> {
        self.checkpoint(true)?;
        let workers = crate::scan_pool::workers()?;
        self.diagnostics.workers = workers;
        let mut pool: Option<Pool<EntryJob, EntryOutput>> = None;
        let (mut masks, mut git_paths, mut identities) = (Vec::new(), Vec::new(), Vec::new());
        for root in self.roots.clone() {
            self.checkpoint(false)?;
            self.active_root = root.clone();
            self.values.add(Metric::Roots, 1.0);
            let fixed = self.protected_paths.iter().any(|p| root.starts_with(p));
            let name = root.file_name().map(|n| n.as_bytes()).unwrap_or(b"");
            let git = name.eq_ignore_ascii_case(b".git") && self.git_read && !fixed;
            let masked = fixed || (self.protected(name)? && !git);
            if masked {
                masks.push(root.clone());
                if !self.validates(&root) {
                    continue;
                }
            }
            if git {
                git_paths.push(root.clone());
            }
            if self.pruned.contains(&root) || !self.is_type(&root, true)? {
                continue;
            }
            let canonical = self.canonical(&root)?;
            let info = self.metadata(&root, true)?;
            identities.push((root.clone(), canonical.clone(), info.dev(), info.ino()));
            if root != canonical {
                self.values.add(Metric::AliasRoots, 1.0);
            }
            let reuse = !(root.starts_with(&self.workspace)
                || canonical.starts_with(&self.workspace)
                || self.workspace.starts_with(&canonical));
            let mount = self.containing(&canonical);
            let canonical_root = canonical;
            let mut paths = Paths::default();
            let mut pending = vec![(paths.root(), masked, mount)];
            self.diagnostics.pending_tasks_peak = self.diagnostics.pending_tasks_peak.max(1);
            let (mut directory, mut canonical, mut scratch) =
                (PathBuf::new(), PathBuf::new(), Vec::new());
            while !pending.is_empty() || pool.as_ref().is_some_and(|p| p.outstanding != 0) {
                if pool.is_none() && workers > 1 && pending.len() >= 2 {
                    pool = Some(Pool::new(
                        workers,
                        |stop| self.worker(stop),
                        scan_directory,
                    )?);
                }
                let mut ready = None;
                while !pending.is_empty() && pool.as_ref().is_none_or(|p| p.outstanding < workers) {
                    self.checkpoint(false)?;
                    let (node, masked, mut mount) = pending.pop().unwrap();
                    paths.write_path(node, &root, &mut directory, &mut scratch);
                    paths.write_path(node, &canonical_root, &mut canonical, &mut scratch);
                    self.diagnostics.path_materialized_bytes +=
                        directory.as_os_str().len() + canonical.as_os_str().len();
                    if self.pruned.contains(&directory) {
                        paths.release(node);
                        continue;
                    }
                    if let Some(index) = self.mount_index.get(&canonical).copied() {
                        mount = Some(index);
                    }
                    self.set_bucket(&root, mount);
                    let job = EntryJob {
                        node,
                        masked,
                        mount,
                        directory: directory.clone(),
                        canonical: canonical.clone(),
                        root: root.clone(),
                    };
                    if pool.is_none() || (reuse && self.cache.contains_key(&canonical)) {
                        self.diagnostics.in_flight_peak = self.diagnostics.in_flight_peak.max(1);
                        let facts = self.entries(&directory, &canonical, reuse)?;
                        ready = Some((job, facts));
                        break;
                    }
                    let pool = pool.as_mut().unwrap();
                    pool.submit(job)?;
                    self.diagnostics.in_flight_peak =
                        self.diagnostics.in_flight_peak.max(pool.outstanding);
                    // At most one DIR/fd per in-flight worker; includes completed jobs conservatively.
                    self.diagnostics.directory_handles_peak = self
                        .diagnostics
                        .directory_handles_peak
                        .max(pool.outstanding);
                }
                let (job, facts) = if let Some(ready) = ready {
                    ready
                } else if let Some(pool) = pool.as_mut().filter(|p| p.outstanding != 0) {
                    let started = Instant::now();
                    let output = pool.receive(|| self.checkpoint(false));
                    self.parallel_wait_ms += elapsed(started);
                    let output = output?;
                    self.set_bucket(&root, output.job.mount);
                    self.merge_values(output.values);
                    self.diagnostics.enumeration_entries_peak = self
                        .diagnostics
                        .enumeration_entries_peak
                        .max(output.diagnostics.enumeration_entries_peak);
                    self.diagnostics.enumeration_buffer_bytes_peak = self
                        .diagnostics
                        .enumeration_buffer_bytes_peak
                        .max(output.diagnostics.enumeration_buffer_bytes_peak);
                    let facts = output.facts?;
                    if reuse {
                        self.cache
                            .insert(output.job.canonical.clone(), facts.clone());
                    }
                    (output.job, facts)
                } else {
                    continue;
                };
                let EntryJob {
                    node,
                    masked,
                    mount,
                    directory,
                    ..
                } = job;
                let entries = match &*facts {
                    Facts::Denied(n) => {
                        if directory.starts_with(&self.workspace)
                            || self.canonical(&directory)?.starts_with(&self.workspace)
                        {
                            return Err(Error::Io(*n, Some(directory.clone())));
                        }
                        masks.push(directory.clone());
                        paths.release(node);
                        continue;
                    }
                    Facts::Entries(entries) => entries,
                };
                for (index, entry) in entries.iter().enumerate() {
                    if index % 1024 == 0 {
                        self.checkpoint(false)?;
                    }
                    let mut child_masked = masked;
                    let fixed = self
                        .protected_children
                        .get(&directory)
                        .is_some_and(|names| names.contains(&entry.name));
                    let git = entry.name.as_bytes().eq_ignore_ascii_case(b".git")
                        && self.git_read
                        && !fixed;
                    let path = directory.join(&entry.name);
                    if !masked && (fixed || (entry.protected && !git)) {
                        masks.push(if entry.link && !path.starts_with(&self.workspace) {
                            directory.clone()
                        } else {
                            path.clone()
                        });
                        child_masked = true;
                    } else if !masked && git {
                        git_paths.push(path.clone());
                    }
                    if entry.directory && (!child_masked || self.validates(&directory)) {
                        pending.push((
                            paths.child(node, entry.name.as_bytes()),
                            child_masked,
                            mount,
                        ));
                        self.diagnostics.pending_tasks_peak =
                            self.diagnostics.pending_tasks_peak.max(pending.len());
                    }
                }
                paths.release(node);
            }
            self.diagnostics.observe_paths(&paths);
        }
        drop(pool); // Join all workers before validating identities and publishing policy.
        self.verify_roots(identities)?;
        let fresh = if cfg!(target_os = "linux") {
            fs::read("/proc/self/mountinfo")
                .map_err(|e| Error::io(e, Some(Path::new("/proc/self/mountinfo"))))?
        } else {
            Vec::new()
        };
        if fresh != self.snapshot {
            return Err(Error::value(
                "Linux native 扫描期间挂载布局发生变化，拒绝使用旧策略",
                None,
            ));
        }
        let masks = outermost(masks)?;
        if workers > 1 {
            // Completion order must never become bind mount order. Preserve all git
            // paths (including nested ones), sorting without outermost pruning.
            let mut keyed = git_paths
                .into_iter()
                .map(|p| Ok((unicode_key(&p)?, p)))
                .collect::<Result<Vec<_>>>()?;
            keyed.sort_by(|a, b| a.0.cmp(&b.0));
            git_paths = keyed.into_iter().map(|(_, p)| p).collect();
        }
        for path in masks.iter().chain(&git_paths) {
            self.checkpoint(false)?;
            if self.is_type(path, false)? {
                return Err(Error::value(
                    "Linux native 受保护挂载点不能是符号链接：",
                    Some(path),
                ));
            }
        }
        self.checkpoint(true)?;
        self.values.set(Metric::Masks, masks.len() as f64);
        self.values.set(Metric::GitPaths, git_paths.len() as f64);
        self.complete = true;
        Ok((masks, git_paths))
    }
    fn verify_roots(&mut self, identities: Vec<(PathBuf, PathBuf, u64, u64)>) -> Result<()> {
        for (root, canonical, device, inode) in identities {
            self.checkpoint(false)?;
            let info = self.metadata(&root, true)?;
            if self.canonical(&root)? != canonical || (info.dev(), info.ino()) != (device, inode) {
                return Err(Error::value(
                    "Linux native 扫描期间挂载目录发生变化：",
                    Some(&root),
                ));
            }
        }
        Ok(())
    }
    pub fn finish(&mut self, start: Instant) {
        let total = elapsed(start);
        let measured = self.values.get(Metric::EnumerationMs)
            + self.values.get(Metric::RulesMs)
            + self.values.get(Metric::WorkspaceValidationMs)
            + self.values.get(Metric::MetadataMs);
        self.values.set(Metric::ScanMs, total);
        self.values.set(
            Metric::MappingMs,
            if self.diagnostics.workers > 1 {
                // Parallel phase timings are cumulative worker time, not wall-time components.
                (total - self.parallel_wait_ms).max(0.0)
            } else {
                (total - measured).max(0.0)
            },
        );
        self.cache.clear();
    }
    pub fn diagnostics(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        self.diagnostics.to_python(py)
    }
    pub fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let metrics = PyDict::new(py);
        self.values.populate(&metrics)?;
        metrics.set_item("complete", self.complete)?;
        let buckets = PyList::empty(py);
        for bucket in &self.buckets {
            let d = PyDict::new(py);
            bucket.values.populate(&d)?;
            d.set_item("root", bucket.root.clone().into_os_string())?;
            d.set_item(
                "mount",
                bucket
                    .mount
                    .map(|i| self.mounts[i].path.clone().into_os_string()),
            )?;
            d.set_item(
                "filesystem",
                bucket.mount.map(|i| &self.mounts[i].filesystem),
            )?;
            buckets.append(d)?;
        }
        metrics.set_item("by_root_mount", buckets)?;
        Ok(metrics)
    }
}
fn elapsed(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

// Python orders decoded path strings, including U+DC80..U+DCFF for invalid bytes.
fn unicode_key(path: &Path) -> Result<Vec<u32>> {
    let bytes = path.as_os_str().as_bytes();
    if bytes.is_ascii() {
        return Ok(bytes.iter().map(|b| u32::from(*b)).collect());
    }
    Python::attach(|py| {
        let encoded: Vec<u8> = py
            .import("os")?
            .call_method1("fsdecode", (pyo3::types::PyBytes::new(py, bytes),))?
            .call_method1("encode", ("utf-32-le", "surrogatepass"))?
            .extract()?;
        Ok(encoded
            .chunks_exact(4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
            .collect())
    })
}
fn outermost(paths: Vec<PathBuf>) -> Result<Vec<PathBuf>> {
    let mut keyed = paths
        .into_iter()
        .map(|p| Ok((p.components().count(), unicode_key(&p)?, p)))
        .collect::<Result<Vec<_>>>()?;
    keyed.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    let mut selected = HashSet::new();
    let mut result = Vec::new();
    for (_, _, path) in keyed {
        if !selected.contains(&path) && !path.ancestors().skip(1).any(|p| selected.contains(p)) {
            selected.insert(path.clone());
            result.push(path);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);

    struct Tree(PathBuf);
    impl Tree {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "policy-scan-rust-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            Self(root.canonicalize().unwrap())
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn scanner(root: &Path) -> Scanner {
        Scanner::new(
            root.join("workspace"),
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            false,
            vec![],
            vec![],
            false,
            None,
        )
    }

    #[test]
    fn replaced_descendant_is_not_followed() {
        let tree = Tree::new();
        let original = tree.0.join("child");
        fs::create_dir(&original).unwrap();
        let mut scan = scanner(&tree.0);
        scan.active_root = tree.0.clone();
        fs::rename(&original, tree.0.join("moved")).unwrap();
        symlink(tree.0.join("moved"), &original).unwrap();
        assert!(matches!(
            scan.entries(&original, &original, false),
            Err(Error::Value(_, _))
        ));
    }

    #[test]
    fn cached_alias_rechecks_descendant_type() {
        let tree = Tree::new();
        let original = tree.0.join("child");
        fs::create_dir(&original).unwrap();
        let mut scan = scanner(&tree.0);
        scan.active_root = tree.0.clone();
        scan.entries(&original, &original, true).unwrap();
        fs::rename(&original, tree.0.join("moved")).unwrap();
        symlink(tree.0.join("moved"), &original).unwrap();
        assert!(matches!(
            scan.entries(&original, &original, true),
            Err(Error::Value(_, _))
        ));
    }

    #[test]
    fn retargeted_root_is_rejected_after_scan() {
        let tree = Tree::new();
        let first = tree.0.join("first");
        let second = tree.0.join("second");
        let alias = tree.0.join("alias");
        fs::create_dir(&first).unwrap();
        fs::create_dir(&second).unwrap();
        symlink(&first, &alias).unwrap();
        let info = fs::metadata(&alias).unwrap();
        let observed = vec![(alias.clone(), first, info.dev(), info.ino())];
        fs::remove_file(&alias).unwrap();
        symlink(&second, &alias).unwrap();
        assert!(matches!(
            scanner(&tree.0).verify_roots(observed),
            Err(Error::Value(_, _))
        ));
    }

    #[test]
    fn path_pruning_uses_components_and_surrogateescape_order() {
        let invalid = PathBuf::from(OsString::from_vec(b"/x/\x80.key".to_vec()));
        assert_eq!(
            outermost(vec![
                PathBuf::from("/x/a/file"),
                invalid.clone(),
                PathBuf::from("/x/é.key"),
                PathBuf::from("/x/ab"),
                PathBuf::from("/x/a")
            ])
            .unwrap(),
            vec![
                PathBuf::from("/x/a"),
                PathBuf::from("/x/ab"),
                PathBuf::from("/x/é.key"),
                invalid
            ]
        );
    }
}
