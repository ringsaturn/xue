/** Error + JSON response helpers shared by the API handlers. */

export class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

const CORS = { "access-control-allow-origin": "*" } as const;

export function json(
  value: unknown,
  init: { status?: number; cacheControl?: string; headers?: Record<string, string> } = {},
): Response {
  const headers: Record<string, string> = {
    "content-type": "application/json; charset=utf-8",
    ...CORS,
    ...init.headers,
  };
  if (init.cacheControl) headers["cache-control"] = init.cacheControl;
  return new Response(JSON.stringify(value), { status: init.status ?? 200, headers });
}

export function errorResponse(error: unknown): Response {
  if (error instanceof HttpError) {
    return json(
      { error: { code: error.code, message: error.message, ...(error.detail === undefined ? {} : { detail: error.detail }) } },
      { status: error.status },
    );
  }
  console.error("[api]", error);
  return json(
    { error: { code: "internal", message: error instanceof Error ? error.message : String(error) } },
    { status: 500 },
  );
}
