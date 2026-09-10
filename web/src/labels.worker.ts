/**
 * The contour-label worker: one message in (a copy of the displayed plane
 * and the request describing it), one message out (lines and centers in
 * degrees). It exists so `isolines.ts` never runs on the main thread, where
 * a whole-world trace would cost a frame or two of playback; here it costs
 * nothing the map can see. Requests carry an id and the main thread keeps
 * only the newest answer, so a burst of frame steps settles on the last.
 */

import { computeLabels, type LabelRequest, type LabelResult } from "./isolines";

export interface LabelsWorkerRequest {
  requestId: number;
  buffer: ArrayBuffer;
  request: LabelRequest;
}

export interface LabelsWorkerResponse {
  requestId: number;
  result: LabelResult;
}

self.onmessage = (event: MessageEvent<LabelsWorkerRequest>) => {
  const { requestId, buffer, request } = event.data;
  const result = computeLabels(new Uint8Array(buffer), request);
  const response: LabelsWorkerResponse = { requestId, result };
  (self as unknown as DedicatedWorkerGlobalScope).postMessage(response);
};
