// WebCodecs random-access spike.
//
// Loads a GOP-6 lossless H.264 Annex-B stream (produced by
// scripts/prep_webcodecs_spike.py) and, for a set of target frame indices,
// decodes from the nearest preceding keyframe up to the target using
// VideoDecoder, extracts the luma plane bytes via VideoFrame.copyTo(), and
// compares them byte-for-byte against the original quantized R8 codes.
//
// This tests two things the format design left unresolved:
//   1. Does the browser's WebCodecs implementation even support the H.264
//      profile libx264 emits for lossless monochrome (typically High 4:4:4
//      Predictive, profile_idc 244)?
//   2. What's the real random-access latency for a GOP-6 seek, and is the
//      decoded luma plane bit-exact against the quantized source?

function out(text) {
  document.getElementById("out").textContent = text;
}

function finish(results) {
  const text = JSON.stringify(results, null, 2);
  console.log("SPIKE_RESULT_JSON_START");
  console.log(text);
  console.log("SPIKE_RESULT_JSON_END");
  out(text);
}

async function main() {
  const results = {};

  const index = await (await fetch("frame_index.json")).json();
  results.index = {
    codecString: index.codecString,
    width: index.width,
    height: index.height,
    frameCount: index.frameCount,
    gop: index.gop,
  };

  const config = {
    codec: index.codecString,
    codedWidth: index.width,
    codedHeight: index.height,
    avc: { format: "annexb" },
  };

  const support = await VideoDecoder.isConfigSupported(config);
  results.configSupported = support.supported;
  console.log("config support:", JSON.stringify(support));

  if (!support.supported) {
    results.conclusion = "VideoDecoder does not support this codec configuration in this browser.";
    finish(results);
    return;
  }

  const [streamBuf, rawBuf] = await Promise.all([
    fetch(index.streamFile).then((r) => r.arrayBuffer()),
    fetch(index.rawFile).then((r) => r.arrayBuffer()),
  ]);

  const frameBytes = index.width * index.height;

  function chunkFor(i) {
    const f = index.frames[i];
    return new EncodedVideoChunk({
      type: f.keyframe ? "key" : "delta",
      timestamp: i,
      data: streamBuf.slice(f.offset, f.offset + f.length),
    });
  }

  function nearestKeyframe(target) {
    for (let i = target; i >= 0; i--) {
      if (index.frames[i].keyframe) return i;
    }
    return 0;
  }

  async function decodeTo(target) {
    return new Promise((resolve, reject) => {
      let targetFrame = null;
      const decoder = new VideoDecoder({
        output(frame) {
          if (frame.timestamp === target) {
            targetFrame = frame;
          } else {
            frame.close();
          }
        },
        error(e) {
          reject(e);
        },
      });
      decoder.configure(config);

      const from = nearestKeyframe(target);
      const start = performance.now();
      for (let i = from; i <= target; i++) decoder.decode(chunkFor(i));

      decoder
        .flush()
        .then(async () => {
          const elapsedMs = performance.now() - start;
          if (!targetFrame) {
            decoder.close();
            reject(new Error(`target frame ${target} was never output (decoded from ${from})`));
            return;
          }

          const rect = { x: 0, y: 0, width: targetFrame.codedWidth, height: targetFrame.codedHeight };
          const size = targetFrame.allocationSize({ rect });
          const dest = new Uint8Array(size);
          const layout = await targetFrame.copyTo(dest, { rect });
          const format = targetFrame.format;
          const stride = layout[0].stride;
          const planeOffset = layout[0].offset;

          const packed = new Uint8Array(frameBytes);
          for (let row = 0; row < index.height; row++) {
            const rowStart = planeOffset + row * stride;
            packed.set(dest.subarray(rowStart, rowStart + index.width), row * index.width);
          }

          const expected = new Uint8Array(rawBuf, target * frameBytes, frameBytes);
          let mismatches = 0;
          for (let k = 0; k < frameBytes; k++) {
            if (packed[k] !== expected[k]) mismatches++;
          }

          targetFrame.close();
          decoder.close();
          resolve({
            target,
            fromKeyframe: from,
            framesDecoded: target - from + 1,
            elapsedMs,
            format,
            mismatches,
            byteExact: mismatches === 0,
          });
        })
        .catch(reject);
    });
  }

  const targets = new Set([0, 1, 5, 6, 7, 30, 60, 61, 90, 119, index.frameCount - 1]);
  while (targets.size < 26) {
    targets.add(Math.floor(Math.random() * index.frameCount));
  }

  const runs = [];
  for (const t of targets) {
    console.log(`decoding target frame ${t} ...`);
    try {
      runs.push(await decodeTo(t));
    } catch (e) {
      runs.push({ target: t, error: String((e && e.message) || e) });
    }
  }

  results.runs = runs;
  const latencies = runs
    .filter((r) => typeof r.elapsedMs === "number")
    .map((r) => r.elapsedMs)
    .sort((a, b) => a - b);
  const byteExactCount = runs.filter((r) => r.byteExact).length;

  results.summary = {
    totalRuns: runs.length,
    errored: runs.filter((r) => r.error).length,
    byteExactCount,
    byteExactAll: byteExactCount === runs.length,
    latencyMs:
      latencies.length > 0
        ? {
            p50: latencies[Math.floor(latencies.length * 0.5)],
            p95: latencies[Math.floor(latencies.length * 0.95)],
            max: latencies[latencies.length - 1],
          }
        : null,
  };

  finish(results);
}

main().catch((e) => {
  console.error("SPIKE_FAILED", e);
  out("FAILED: " + (e && e.stack ? e.stack : e));
});
