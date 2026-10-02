//! Per-call bounded executor. Only the coordinator calls Python cancellation.
use crate::{Error, Result};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub(crate) fn workers() -> Result<usize> {
    match std::env::var("AGENT_SCAN_WORKERS") {
        Err(std::env::VarError::NotPresent) => Ok(2),
        Ok(value) if value.is_empty() => Ok(2),
        Ok(value) => value
            .parse::<usize>()
            .ok()
            .filter(|n| (1..=8).contains(n))
            .ok_or_else(|| Error::value("AGENT_SCAN_WORKERS 必须为 1 到 8 的整数", None)),
        Err(_) => Err(Error::value(
            "AGENT_SCAN_WORKERS 必须为 1 到 8 的整数",
            None,
        )),
    }
}
pub(crate) fn checkpoint(stop: &AtomicBool) -> Result<()> {
    if stop.load(Ordering::Relaxed) {
        Err(Error::value("Native scan stopped", None))
    } else {
        Ok(())
    }
}

pub(crate) struct Pool<J, O> {
    sender: Option<mpsc::SyncSender<J>>,
    receiver: mpsc::Receiver<Result<O>>,
    handles: Vec<JoinHandle<()>>,
    stop: Arc<AtomicBool>,
    pub outstanding: usize,
    limit: usize,
}
impl<J: Send + 'static, O: Send + 'static> Pool<J, O> {
    pub fn new<S: Send + 'static>(
        limit: usize,
        mut state: impl FnMut(Arc<AtomicBool>) -> S,
        work: fn(&mut S, J, &AtomicBool) -> Result<O>,
    ) -> Result<Self> {
        let (sender, jobs) = mpsc::sync_channel::<J>(limit);
        let (results, receiver) = mpsc::channel();
        let jobs = Arc::new(Mutex::new(jobs));
        let mut pool = Self {
            sender: Some(sender),
            receiver,
            handles: Vec::new(),
            stop: Arc::new(AtomicBool::new(false)),
            outstanding: 0,
            limit,
        };
        for index in 0..limit {
            let (jobs, results, stop) = (jobs.clone(), results.clone(), pool.stop.clone());
            let mut state = state(pool.stop.clone());
            let handle = thread::Builder::new()
                .name(format!("native-scan-{index}"))
                .spawn(move || loop {
                    // Lock protects receiving only, never I/O, Python or result delivery.
                    let job = jobs.lock().unwrap().recv();
                    let Ok(job) = job else { break };
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        work(&mut state, job, &stop)
                    }))
                    .unwrap_or_else(|_| Err(Error::value("Native scan worker panicked", None)));
                    let failed = result.is_err();
                    if results.send(result).is_err() {
                        break;
                    }
                    // Coordinator receives the original error, then stops and joins all workers.
                    if failed {
                        break;
                    }
                })
                .map_err(|e| Error::io(e, None))?;
            pool.handles.push(handle);
        }
        Ok(pool)
    }
    pub fn submit(&mut self, job: J) -> Result<()> {
        assert!(self.outstanding < self.limit);
        self.sender
            .as_ref()
            .unwrap()
            .send(job)
            .map_err(|_| Error::value("Native scan workers disconnected", None))?;
        self.outstanding += 1;
        Ok(())
    }
    pub fn receive(&mut self, mut check: impl FnMut() -> Result<()>) -> Result<O> {
        loop {
            check()?;
            match self.receiver.recv_timeout(Duration::from_millis(10)) {
                Ok(result) => {
                    self.outstanding -= 1;
                    return result;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => (),
                Err(_) => return Err(Error::value("Native scan workers disconnected", None)),
            }
        }
    }
}
impl<J, O> Drop for Pool<J, O> {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.sender.take();
        for handle in self.handles.drain(..) {
            let _ = handle.join();
        }
        // Queued jobs and results own their resources and are dropped with the channels.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    #[test]
    fn completion_is_unordered_and_window_includes_unreceived_results() {
        struct State(Arc<AtomicBool>, Arc<AtomicUsize>);
        let release = Arc::new(AtomicBool::new(false));
        let entered = Arc::new(AtomicUsize::new(0));
        let mut pool = Pool::new(
            2,
            |_| State(release.clone(), entered.clone()),
            |s, job, stop| {
                s.1.fetch_add(1, Ordering::SeqCst);
                if job == 0 {
                    while !s.0.load(Ordering::SeqCst) {
                        checkpoint(stop)?;
                        thread::sleep(Duration::from_millis(1));
                    }
                }
                Ok(job)
            },
        )
        .unwrap();
        pool.submit(0).unwrap();
        pool.submit(1).unwrap();
        assert_eq!(pool.outstanding, 2);
        assert_eq!(pool.receive(|| Ok(())).unwrap(), 1);
        assert_eq!(pool.outstanding, 1);
        release.store(true, Ordering::SeqCst);
        assert_eq!(pool.receive(|| Ok(())).unwrap(), 0);
        assert_eq!(entered.load(Ordering::SeqCst), 2);
    }
    #[test]
    fn cancelled_receive_joins_running_workers_and_drops_resources() {
        struct Resource(Arc<AtomicUsize>);
        impl Drop for Resource {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::SeqCst);
            }
        }
        let dropped = Arc::new(AtomicUsize::new(0));
        let entered = Arc::new(AtomicUsize::new(0));
        let mut pool = Pool::new(
            2,
            |_| entered.clone(),
            |entered, _job: Resource, stop| -> Result<()> {
                entered.fetch_add(1, Ordering::SeqCst);
                loop {
                    checkpoint(stop)?;
                    thread::sleep(Duration::from_millis(1));
                }
            },
        )
        .unwrap();
        pool.submit(Resource(dropped.clone())).unwrap();
        pool.submit(Resource(dropped.clone())).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while entered.load(Ordering::SeqCst) != 2 {
            assert!(std::time::Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        assert!(pool.receive(|| Err(Error::value("cancel", None))).is_err());
        drop(pool);
        assert_eq!(dropped.load(Ordering::SeqCst), 2);
    }
    #[test]
    fn panic_is_an_error_instead_of_a_missing_result() {
        let mut pool = Pool::new(
            2,
            |_| (),
            |_, _: (), _| -> Result<()> { panic!("test worker") },
        )
        .unwrap();
        pool.submit(()).unwrap();
        assert!(matches!(pool.receive(|| Ok(())), Err(Error::Value(_, _))));
    }
}
