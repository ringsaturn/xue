//! JSON spelled the way the reference's `json.dumps` spells it, in the two
//! forms the outputs use. Every document compared byte for byte against the
//! Python encoder is written through here.

use serde_json::Value;

/// `json.dumps(value, indent=2, ensure_ascii=False) + "\n"`: two-space
/// indentation, `": "` between a key and its value, raw UTF-8, and `{}` /
/// `[]` for empty containers, which is serde_json's pretty printer with a
/// two-space indent. Numbers are serde_json's, the same spelling the bundle
/// metadata already shares with the reference. The manifest and every Zarr
/// document.
pub(crate) fn dumps_indented(value: &Value) -> String {
    let mut text = serde_json::to_string_pretty(value).expect("serializable JSON");
    text.push('\n');
    text
}

/// Serialize a value the way Python's `json.dumps` does *by default*: one line,
/// a space after every separator, and every non-ASCII character escaped.
///
/// The bundles embed their metadata with compact separators and raw UTF-8, and
/// the encoder matches that with plain `serde_json::to_string`. The
/// `metadataJson` strings the manifest carries for posters and videos are the
/// one place the reference leaves `json.dumps` at its defaults, so they need
/// both the spaced form and `ensure_ascii`.
pub(crate) fn dumps_default(value: &Value) -> String {
    let mut output = Vec::new();
    let mut serializer =
        serde_json::Serializer::with_formatter(&mut output, SpacedFormatter);
    serde::Serialize::serialize(value, &mut serializer).expect("serializable value");
    String::from_utf8(output).expect("UTF-8 JSON")
}

struct SpacedFormatter;

impl serde_json::ser::Formatter for SpacedFormatter {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        writer.write_all(if first { b"" } else { b", " })
    }

    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        writer.write_all(if first { b"" } else { b", " })
    }

    fn begin_object_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
    ) -> std::io::Result<()> {
        writer.write_all(b": ")
    }

    /// `ensure_ascii`: every character outside ASCII becomes a `\uXXXX`
    /// escape, in lowercase hex, an astral one as its UTF-16 surrogate pair.
    /// The degree sign in the temperature unit is the one that shows up in
    /// practice, and the manifest is compared byte for byte.
    fn write_string_fragment<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        fragment: &str,
    ) -> std::io::Result<()> {
        if fragment.is_ascii() {
            return writer.write_all(fragment.as_bytes());
        }
        let mut start = 0;
        for (index, character) in fragment.char_indices() {
            if character.is_ascii() {
                continue;
            }
            writer.write_all(&fragment.as_bytes()[start..index])?;
            start = index + character.len_utf8();
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                write!(writer, "\\u{unit:04x}")?;
            }
        }
        writer.write_all(&fragment.as_bytes()[start..])
    }
}

#[cfg(test)]
mod tests {
    use super::{dumps_default, dumps_indented};
    use serde_json::json;

    #[test]
    fn json_is_spelled_the_way_python_indents_it() {
        let value = json!({"a": [1, 2.5, "°C"], "b": {}, "c": [], "d": {"e": true}});
        let text = dumps_indented(&value);
        assert_eq!(
            text,
            "{\n  \"a\": [\n    1,\n    2.5,\n    \"°C\"\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": {\n    \"e\": true\n  }\n}\n"
        );
    }

    #[test]
    fn spaced_json_matches_python_json_dumps_defaults() {
        // The manifest's poster and video descriptors carry a variable's metadata
        // as a string written by `json.dumps` with nothing overridden: a space
        // after every separator, and every non-ASCII character escaped. The degree
        // sign in the temperature unit is the one that occurs in practice.
        let value = serde_json::json!({
            "unit": "°C",
            "labels": ["雪", "\u{1f300}"],
            "quoted": "a \"b\"\n",
            "plain": 1,
        });
        assert_eq!(
            dumps_default(&value),
            "{\"unit\": \"\\u00b0C\", \"labels\": [\"\\u96ea\", \"\\ud83c\\udf00\"], \
             \"quoted\": \"a \\\"b\\\"\\n\", \"plain\": 1}"
        );
    }
}
