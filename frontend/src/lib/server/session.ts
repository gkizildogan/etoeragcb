import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { Cookies } from '@sveltejs/kit';

export const SESSION_COOKIE = '__Host-rag_session';
const AAD = Buffer.from('etoeragcb:session:v1', 'utf8');

export interface TokenBundle {
  version: 1;
  accessToken: string;
  refreshToken: string;
  tenantId: string;
  accessExpiresAt: number;
  hardExpiresAt: number;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  tenant_id: string;
  expires_in: number;
  refresh_expires_in: number;
}

let cachedKey: Buffer | undefined;

export function tokenBundle(payload: TokenResponse, now = Date.now()): TokenBundle {
  return {
    version: 1,
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    tenantId: payload.tenant_id,
    accessExpiresAt: now + payload.expires_in * 1000,
    hardExpiresAt: now + payload.refresh_expires_in * 1000
  };
}

export function loadSessionKey(): Buffer {
  if (cachedKey) return cachedKey;
  const environmentSecret = process.env.FRONTEND_SESSION_SECRET;
  const source =
    environmentSecret ??
    readFileSync(
      process.env.FRONTEND_SESSION_SECRET_FILE ?? '/run/secrets/frontend_session_secret',
      {
        encoding: 'utf8'
      }
    ).trim();
  const decoded = decodeSecret(source);
  if (decoded.length !== 32) {
    throw new Error('frontend session secret must contain exactly 32 bytes');
  }
  cachedKey = decoded;
  return decoded;
}

export function sealSession(bundle: TokenBundle, key = loadSessionKey()): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(AAD);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(bundle), 'utf8'), cipher.final()]);
  return [
    '1',
    iv.toString('base64url'),
    ciphertext.toString('base64url'),
    cipher.getAuthTag().toString('base64url')
  ].join('.');
}

export function openSession(
  value: string | undefined,
  key = loadSessionKey(),
  now = Date.now()
): TokenBundle | null {
  if (!value) return null;
  const parts = value.split('.');
  if (parts.length !== 4 || parts[0] !== '1') return null;
  try {
    const iv = Buffer.from(parts[1] ?? '', 'base64url');
    const ciphertext = Buffer.from(parts[2] ?? '', 'base64url');
    const tag = Buffer.from(parts[3] ?? '', 'base64url');
    if (iv.length !== 12 || tag.length !== 16 || ciphertext.length === 0) return null;
    const decipher = createDecipheriv('aes-256-gcm', key, iv);
    decipher.setAAD(AAD);
    decipher.setAuthTag(tag);
    const raw = Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
    const bundle = parseBundle(JSON.parse(raw) as unknown);
    return bundle && bundle.hardExpiresAt > now ? bundle : null;
  } catch {
    return null;
  }
}

export function setSessionCookie(cookies: Cookies, bundle: TokenBundle): void {
  cookies.set(SESSION_COOKIE, sealSession(bundle), {
    expires: new Date(bundle.hardExpiresAt),
    httpOnly: true,
    path: '/',
    sameSite: 'lax',
    secure: true
  });
}

export function clearSessionCookie(cookies: Cookies): void {
  cookies.delete(SESSION_COOKIE, {
    path: '/',
    httpOnly: true,
    sameSite: 'lax',
    secure: true
  });
}

function parseBundle(value: unknown): TokenBundle | null {
  if (!isRecord(value)) return null;
  if (
    value.version !== 1 ||
    !nonEmpty(value.accessToken) ||
    !nonEmpty(value.refreshToken) ||
    !uuid(value.tenantId) ||
    !positiveNumber(value.accessExpiresAt) ||
    !positiveNumber(value.hardExpiresAt) ||
    value.accessExpiresAt > value.hardExpiresAt
  ) {
    return null;
  }
  return value as unknown as TokenBundle;
}

function decodeSecret(value: string): Buffer {
  if (/^[0-9a-fA-F]{64}$/.test(value)) return Buffer.from(value, 'hex');
  const base64 = Buffer.from(value, 'base64');
  if (base64.length === 32) return base64;
  return Buffer.from(value, 'utf8');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function uuid(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

function positiveNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}
