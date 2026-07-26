import { createHash } from 'node:crypto';
import type { RequestEvent } from '@sveltejs/kit';
import { clearSessionCookie, setSessionCookie, tokenBundle, type TokenBundle } from './session';
import { parseTokenResponse, validateApiPayload } from './validation';

const API_INTERNAL_URL = (process.env.API_INTERNAL_URL ?? 'http://backend:8000/api').replace(
  /\/$/,
  ''
);
const REQUEST_TIMEOUT_MS = 15_000;
const REFRESH_CACHE_MS = 10_000;
const MAX_ROTATED_RESULTS = 128;

interface RotatedResult {
  bundle: TokenBundle;
  expiresAt: number;
}

const refreshes = new Map<string, Promise<TokenBundle>>();
const rotated = new Map<string, RotatedResult>();

export class GatewayError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    readonly retryAfter?: number
  ) {
    super(code);
  }
}

export async function loginUpstream(
  email: string,
  password: string,
  tenantId?: string
): Promise<TokenBundle> {
  let response: Response;
  try {
    response = await fetch(`${API_INTERNAL_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email,
        password,
        ...(tenantId ? { tenant_id: tenantId } : {})
      }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    });
  } catch {
    throw new GatewayError('service_unavailable', 503);
  }
  if (!response.ok) throw mapUpstreamError(response, 'login_failed');
  try {
    return tokenBundle(parseTokenResponse(await response.json()));
  } catch {
    throw new GatewayError('invalid_upstream_response', 502);
  }
}

export async function apiJson<T = unknown>(
  event: RequestEvent,
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await apiRaw(event, path, init);
  if (response.status === 204) return null as T;
  try {
    const validationPath = path.split('?', 1)[0] ?? path;
    return validateApiPayload(validationPath, init.method ?? 'GET', await response.json()) as T;
  } catch {
    throw new GatewayError('invalid_upstream_response', 502);
  }
}

export async function apiRaw(
  event: RequestEvent,
  path: string,
  init: RequestInit = {},
  timeoutMs = REQUEST_TIMEOUT_MS
): Promise<Response> {
  let bundle = event.locals.session;
  if (!bundle || bundle.hardExpiresAt <= Date.now()) {
    clearAuthentication(event);
    throw new GatewayError('session_expired', 401);
  }

  let response = await authorizedFetch(path, bundle.accessToken, init, timeoutMs);
  if (response.status === 401) {
    try {
      bundle = await refreshToken(bundle);
    } catch {
      clearAuthentication(event);
      throw new GatewayError('session_expired', 401);
    }
    event.locals.session = bundle;
    setSessionCookie(event.cookies, bundle);
    response = await authorizedFetch(path, bundle.accessToken, init, timeoutMs);
  }
  if (!response.ok) throw mapUpstreamError(response, 'api_request_failed');
  return response;
}

export async function logoutUpstream(event: RequestEvent): Promise<void> {
  const bundle = event.locals.session;
  clearAuthentication(event);
  if (!bundle) return;
  try {
    await fetch(`${API_INTERNAL_URL}/auth/logout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: bundle.refreshToken }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    });
  } catch {
    // Logout is local-first; the refresh token expires server-side if revocation is unavailable.
  }
}

export function publicError(error: unknown): {
  status: number;
  body: { error: { code: string; message: string; retry_after?: number } };
} {
  const mapped =
    error instanceof GatewayError ? error : new GatewayError('service_unavailable', 503);
  const messages: Record<string, string> = {
    api_request_failed: 'The request could not be completed.',
    invalid_upstream_response: 'The service returned an invalid response.',
    login_failed: 'The email, password, or organization is incorrect.',
    service_unavailable: 'The service is temporarily unavailable.',
    session_expired: 'Your session expired. Sign in again.'
  };
  return {
    status: mapped.status,
    body: {
      error: {
        code: mapped.code,
        message: messages[mapped.code] ?? 'The request could not be completed.',
        ...(mapped.retryAfter === undefined ? {} : { retry_after: mapped.retryAfter })
      }
    }
  };
}

async function authorizedFetch(
  path: string,
  accessToken: string,
  init: RequestInit,
  timeoutMs: number
): Promise<Response> {
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new GatewayError('invalid_request', 400);
  }
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${accessToken}`);
  try {
    return await fetch(`${API_INTERNAL_URL}${path}`, {
      ...init,
      headers,
      signal: AbortSignal.timeout(timeoutMs)
    });
  } catch {
    throw new GatewayError('service_unavailable', 503);
  }
}

async function refreshToken(oldBundle: TokenBundle): Promise<TokenBundle> {
  const key = refreshHash(oldBundle.refreshToken);
  pruneRotated();
  const cached = rotated.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.bundle;
  const existing = refreshes.get(key);
  if (existing) return existing;

  const pending = performRefresh(oldBundle).then((bundle) => {
    rotated.set(key, { bundle, expiresAt: Date.now() + REFRESH_CACHE_MS });
    while (rotated.size > MAX_ROTATED_RESULTS) {
      const oldest = rotated.keys().next().value;
      if (!oldest) break;
      rotated.delete(oldest);
    }
    return bundle;
  });
  refreshes.set(key, pending);
  try {
    return await pending;
  } finally {
    refreshes.delete(key);
  }
}

async function performRefresh(oldBundle: TokenBundle): Promise<TokenBundle> {
  const response = await fetch(`${API_INTERNAL_URL}/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: oldBundle.refreshToken }),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
  });
  if (!response.ok) throw new GatewayError('session_expired', 401);
  const replacement = tokenBundle(parseTokenResponse(await response.json()));
  if (replacement.tenantId !== oldBundle.tenantId) {
    throw new GatewayError('session_expired', 401);
  }
  return replacement;
}

function mapUpstreamError(response: Response, fallback: string): GatewayError {
  const retryHeader = response.headers.get('retry-after');
  const retryAfter = retryHeader ? Number.parseInt(retryHeader, 10) : undefined;
  return new GatewayError(
    response.status === 401 ? 'session_expired' : fallback,
    response.status,
    Number.isFinite(retryAfter) ? retryAfter : undefined
  );
}

function clearAuthentication(event: RequestEvent): void {
  event.locals.session = null;
  clearSessionCookie(event.cookies);
}

function refreshHash(token: string): string {
  return createHash('sha256').update(token).digest('hex');
}

function pruneRotated(): void {
  const now = Date.now();
  for (const [key, result] of rotated) {
    if (result.expiresAt <= now) rotated.delete(key);
  }
}
