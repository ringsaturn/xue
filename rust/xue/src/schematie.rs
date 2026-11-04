//! Test support: the committed JSON Schemas (`docs/schemas/`) the readers and
//! writers here are held to by name. The crate has no runtime schema
//! dependency; its tests read the schema files and assert that every field
//! name the code reads or writes is a property the schema names, and that a
//! block the format closes names exactly the fields the code knows.

use std::collections::BTreeSet;

use serde_json::Value;

/// `docs/schemas/<name>.schema.json`, read from the repository.
pub(crate) fn schema(name: &str) -> Value {
    let path = format!("{}/../../docs/schemas/{name}.schema.json", env!("CARGO_MANIFEST_DIR"));
    let text = std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{path}: {error}"));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("{path}: {error}"))
}

/// The properties of the object schema at `pointer` (a JSON pointer into
/// the schema: `""` for the root, `/$defs/Time`, `/properties/attributes`).
fn properties(name: &str, pointer: &str) -> (BTreeSet<String>, Value) {
    let schema = schema(name);
    let node = schema
        .pointer(pointer)
        .unwrap_or_else(|| panic!("{name}.schema.json has nothing at {pointer:?}"))
        .clone();
    let names = node["properties"]
        .as_object()
        .unwrap_or_else(|| panic!("{name}.schema.json#{pointer} names no properties"))
        .keys()
        .cloned()
        .collect();
    (names, node)
}

/// Every one of `fields` is a property of `name`'s schema at `pointer`.
pub(crate) fn assert_named(name: &str, pointer: &str, fields: &[&str]) {
    let (known, _) = properties(name, pointer);
    for field in fields {
        assert!(known.contains(*field), "{field} is not a property of {name}.schema.json#{pointer}");
    }
}

/// The schema at `pointer` closes its object and names exactly `fields`.
pub(crate) fn assert_exactly(name: &str, pointer: &str, fields: &[&str]) {
    let (known, node) = properties(name, pointer);
    let fields: BTreeSet<String> = fields.iter().map(|field| (*field).to_owned()).collect();
    assert_eq!(known, fields, "{name}.schema.json#{pointer}");
    assert_eq!(node["additionalProperties"], Value::Bool(false), "{name}.schema.json#{pointer} must be closed");
}
