"""Regenerate tests/fixtures/satellite-registry.json from the Python encoder.

The fixture holds the satellite channels and the Dust RGB guns — label,
unit, parameter block, band block (channels, per platform) or producer id
(guns), both codebooks — and each composite bundle's components in order,
identical across the Python encoder, the Rust encoder
(``rust/xue/src/encode/variables.rs``) and the shell
(``web/src/variables.ts``). Run it when the registry changes deliberately,
then change the other two.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_satellite import REGISTRY, registry_document  # noqa: E402


def main() -> None:
    REGISTRY.write_text(json.dumps(registry_document(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {REGISTRY}")


if __name__ == "__main__":
    main()
