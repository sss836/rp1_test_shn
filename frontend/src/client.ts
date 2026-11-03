const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
let unauthorizedHandler: (() => void) | null = null;
let authenticationEpoch = 0;

// Invalidates callbacks from requests started under a previous identity.
export function invalidateAuthenticationRequests() {
  authenticationEpoch += 1;
}

const SESSION_INVALIDATION_CODES = new Set([
  "authentication_required",
  "invalid_session",
  "session_expired",
  "session_revoked"
]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(message: string, status: number, code = "api_error", details: unknown = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function isSessionInvalidationError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 401 && SESSION_INVALIDATION_CODES.has(error.code);
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null;
  };
}

function cookieValue(name: string) {
  const prefix = `${encodeURIComponent(name)}=`;
  const match = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : "";
}

export function requestId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join("")
  ].join("-");
}

function headerValue(value: string) {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) > 255) return encodeURIComponent(value);
  }
  return value;
}

function buildHeaders(source: HeadersInit | undefined) {
  const headers = new Headers();
  if (!source) return headers;
  const pairs = source instanceof Headers
    ? source.entries()
    : Array.isArray(source)
      ? source
      : Object.entries(source);
  for (const [name, value] of pairs) headers.set(name, headerValue(String(value)));
  return headers;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  parse?: (data: unknown) => T
): Promise<T> {
  const requestEpoch = authenticationEpoch;
  const requestUnauthorizedHandler = unauthorizedHandler;
  const headers = buildHeaders(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (init.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Request-Id", requestId());
  if (!SAFE_METHODS.has(method) && !headers.has("X-CSRF-Token")) {
    const csrf = cookieValue("rp1_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const configuredDevUserId = import.meta.env.DEV
    ? import.meta.env.VITE_DEV_USER_PUBLIC_ID?.trim()
    : "";
  if (configuredDevUserId) headers.set("X-User-Public-Id", configuredDevUserId);

  const response = await fetch(path, { ...init, credentials: "include", headers });
  if (response.status === 204 && response.ok) return undefined as T;
  const rawBody = await response.text();
  let payload: unknown = null;
  try {
    payload = rawBody ? JSON.parse(rawBody) : null;
  } catch {
    // Converted to a stable error below; response bodies are never logged.
  }
  const record = payload && typeof payload === "object" ? payload as Record<string, unknown> : null;
  const failureRecord = record?.error && typeof record.error === "object"
    ? record.error as Record<string, unknown>
    : null;
  const failure = failureRecord &&
    typeof failureRecord.code === "string" &&
    typeof failureRecord.message === "string"
    ? {
        code: failureRecord.code,
        message: failureRecord.message,
        details: failureRecord.details
      }
    : null;
  if (!response.ok || !record || !("data" in record)) {
    const invalidSuccessResponse = response.ok;
    if (
      response.status === 401 &&
      failure?.code &&
      SESSION_INVALIDATION_CODES.has(failure.code) &&
      !init.signal?.aborted &&
      requestEpoch === authenticationEpoch &&
      requestUnauthorizedHandler === unauthorizedHandler
    ) {
      requestUnauthorizedHandler?.();
    }
    throw new ApiError(
      failure?.message ?? (invalidSuccessResponse
        ? "服务返回了无法识别的数据格式。"
        : `服务暂时不可用（HTTP ${response.status}）。`),
      invalidSuccessResponse ? 502 : response.status,
      failure?.code ?? "invalid_response",
      failure?.details ?? []
    );
  }
  return parse ? parse(record.data) : record.data as T;
}

// XMLHttpRequest is used only for upload byte progress. Keep the same identity,
// Cookie and CSRF rules as apiRequest, including ignoring a late 401 after logout.
export function apiUpload<T>(path: string, file: File, signal: AbortSignal | undefined, onProgress: (loaded: number, total: number) => void): Promise<T> {
  const epoch = authenticationEpoch, handler = unauthorizedHandler;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    if (signal?.aborted) { reject(new DOMException("已取消", "AbortError")); return; }
    xhr.open("POST", path); xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.setRequestHeader("X-Filename-UTF8", encodeURIComponent(file.name));
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("X-Request-Id", requestId());
    const csrf = cookieValue("rp1_csrf"); if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
    const devId = import.meta.env.DEV ? import.meta.env.VITE_DEV_USER_PUBLIC_ID?.trim() : "";
    if (devId) xhr.setRequestHeader("X-User-Public-Id", devId);
    const done = () => signal?.removeEventListener("abort", abort);
    xhr.upload.onprogress = (event) => { if (!signal?.aborted && epoch === authenticationEpoch) onProgress(event.loaded, event.lengthComputable ? event.total : file.size); };
    xhr.onabort = () => { done(); reject(new DOMException("已取消", "AbortError")); };
    xhr.onerror = () => { done(); reject(new ApiError("附件上传连接中断，可重试该文件。", 0, "upload_network")); };
    xhr.onload = () => {
      done();
      if (signal?.aborted || epoch !== authenticationEpoch) { reject(new DOMException("登录身份已变更", "AbortError")); return; }
      let body: { data?: T; error?: { code?: string; message?: string; details?: unknown } } | null = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* Stable error below. */ }
      if (xhr.status >= 200 && xhr.status < 300 && body && "data" in body) { onProgress(file.size, file.size); resolve(body.data as T); return; }
      if (xhr.status === 401 && body?.error?.code && SESSION_INVALIDATION_CODES.has(body.error.code) && handler === unauthorizedHandler) handler?.();
      reject(new ApiError(body?.error?.message ?? `附件上传失败（HTTP ${xhr.status}）。`, xhr.status >= 200 && xhr.status < 300 ? 502 : xhr.status, body?.error?.code ?? "upload_failed", body?.error?.details));
    };
    signal?.addEventListener("abort", abort, { once: true }); xhr.send(file);
  });
}
