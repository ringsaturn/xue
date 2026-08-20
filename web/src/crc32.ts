/**
 * Incremental CRC-32/IEEE (polynomial 0xEDB88320, init and final XOR
 * 0xFFFFFFFF) — the same variant as zlib and the Xue manifest contract.
 * The bundle checksum is folded over streamed download chunks so an 80 MB
 * buffer never needs a second full hashing pass.
 */

const TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
})();

export const CRC32_INITIAL = 0xffffffff;

export function crc32Update(state: number, chunk: Uint8Array): number {
  let crc = state >>> 0;
  for (let index = 0; index < chunk.length; index += 1) {
    crc = (crc >>> 8) ^ TABLE[(crc ^ chunk[index]!) & 0xff]!;
  }
  return crc >>> 0;
}

export function crc32Final(state: number): number {
  return (state ^ 0xffffffff) >>> 0;
}

export function crc32Hex(state: number): string {
  return crc32Final(state).toString(16).padStart(8, "0");
}

export function crc32Of(data: Uint8Array): string {
  return crc32Hex(crc32Update(CRC32_INITIAL, data));
}
