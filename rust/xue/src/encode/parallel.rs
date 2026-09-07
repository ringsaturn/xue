//! An in-order worker pool.
//!
//! The extraction stage cannot use a work-stealing pool: on the
//! derived-precipitation sources a file differences against the *previous*
//! file's raw plane, and the worker holding file `i` blocks until file `i - 1`
//! publishes. That is only deadlock-free if tasks are claimed in ascending
//! order, which is exactly what Python's `ThreadPoolExecutor` FIFO gives the
//! reference encoder — a claimed task's predecessor is always already running
//! or done. Rayon makes no such promise, so the extract stage uses this.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// Run `task` over `0..count` on `workers` threads, claiming indices in
/// ascending order. Results come back in index order.
pub fn for_each_ordered<T, E>(
    count: usize,
    workers: usize,
    task: impl Fn(usize) -> Result<T, E> + Sync,
) -> Result<Vec<T>, E>
where
    T: Send,
    E: Send,
{
    let slots: Mutex<Vec<Option<Result<T, E>>>> =
        Mutex::new((0..count).map(|_| None).collect());
    let cursor = AtomicUsize::new(0);
    let task = &task;
    let slots_ref = &slots;
    let cursor = &cursor;
    std::thread::scope(|scope| {
        for _ in 0..workers.clamp(1, count.max(1)) {
            scope.spawn(move || loop {
                let index = cursor.fetch_add(1, Ordering::SeqCst);
                if index >= count {
                    break;
                }
                let result = task(index);
                slots_ref.lock().expect("slot table")[index] = Some(result);
            });
        }
    });
    slots
        .into_inner()
        .expect("slot table")
        .into_iter()
        .map(|slot| slot.expect("every index was claimed"))
        .collect()
}
