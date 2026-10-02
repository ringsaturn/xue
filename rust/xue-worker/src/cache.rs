//! Isolate-level cache for immutable objects.
//!
//! A store's group and array documents and its shard index are immutable for a
//! run — every read is `?v=<crc32>` addressed — and a live manifest is
//! immutable for a given pointer crc, so an isolate serving repeated queries
//! can answer them without going back to R2. Mutable documents (the Collection
//! and the live pointer) are never cached here. An isolate is ephemeral and a
//! cold one simply reads again, so this is an optimization, never a source of
//! truth.

use std::cell::RefCell;
use std::collections::HashMap;

/// Bytes held across all entries. A GFS store index is ~256 KB and a run's
/// manifests a few hundred KB, so this keeps a working set of a few runs.
const LIMIT_BYTES: usize = 32 * 1024 * 1024;

enum Entry {
    Text(String),
    Bytes(Vec<u8>),
}

impl Entry {
    fn len(&self) -> usize {
        match self {
            Entry::Text(text) => text.len(),
            Entry::Bytes(bytes) => bytes.len(),
        }
    }
}

struct Store {
    map: HashMap<String, Entry>,
    order: Vec<String>,
    bytes: usize,
}

thread_local! {
    static STORE: RefCell<Store> = RefCell::new(Store {
        map: HashMap::new(),
        order: Vec::new(),
        bytes: 0,
    });
}

fn touch(store: &mut Store, key: &str) {
    if let Some(position) = store.order.iter().position(|held| held == key) {
        let key = store.order.remove(position);
        store.order.push(key);
    }
}

fn insert(store: &mut Store, key: String, entry: Entry) {
    if let Some(previous) = store.map.remove(&key) {
        store.bytes -= previous.len();
        if let Some(position) = store.order.iter().position(|held| *held == key) {
            store.order.remove(position);
        }
    }
    store.bytes += entry.len();
    store.order.push(key.clone());
    store.map.insert(key, entry);
    while store.bytes > LIMIT_BYTES && !store.order.is_empty() {
        let oldest = store.order.remove(0);
        if let Some(evicted) = store.map.remove(&oldest) {
            store.bytes -= evicted.len();
        }
    }
}

pub fn text(key: &str) -> Option<String> {
    STORE.with(|cell| {
        let mut store = cell.borrow_mut();
        let value = match store.map.get(key) {
            Some(Entry::Text(text)) => text.clone(),
            _ => return None,
        };
        touch(&mut store, key);
        Some(value)
    })
}

pub fn bytes(key: &str) -> Option<Vec<u8>> {
    STORE.with(|cell| {
        let mut store = cell.borrow_mut();
        let value = match store.map.get(key) {
            Some(Entry::Bytes(bytes)) => bytes.clone(),
            _ => return None,
        };
        touch(&mut store, key);
        Some(value)
    })
}

pub fn put_text(key: &str, value: String) {
    STORE.with(|cell| {
        let mut store = cell.borrow_mut();
        insert(&mut store, key.to_owned(), Entry::Text(value));
    });
}

pub fn put_bytes(key: &str, value: Vec<u8>) {
    STORE.with(|cell| {
        let mut store = cell.borrow_mut();
        insert(&mut store, key.to_owned(), Entry::Bytes(value));
    });
}
