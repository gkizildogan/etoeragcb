import type { TokenResponse } from './session';

export type JsonRecord = Record<string, unknown>;

export function parseTokenResponse(value: unknown): TokenResponse {
  const item = record(value);
  const accessToken = requiredString(item.access_token);
  const refreshToken = requiredString(item.refresh_token);
  const tenantId = requiredUuid(item.tenant_id);
  const expiresIn = positiveInteger(item.expires_in);
  const refreshExpiresIn = positiveInteger(item.refresh_expires_in);
  return {
    access_token: accessToken,
    refresh_token: refreshToken,
    tenant_id: tenantId,
    expires_in: expiresIn,
    refresh_expires_in: refreshExpiresIn
  };
}

export function validateApiPayload(path: string, method: string, value: unknown): unknown {
  if (method === 'DELETE' && value === null) return null;
  const payload = record(value);
  if (path === '/me') {
    requiredString(payload.email);
    requiredUuid(payload.active_tenant_id);
    for (const raw of array(payload.memberships)) {
      const membership = record(raw);
      requiredUuid(membership.tenant_id);
      requiredString(membership.role);
      if (typeof membership.active !== 'boolean') throw new Error('invalid upstream response');
    }
  } else if (path === '/sessions' && method === 'GET') {
    validateItems(payload, (item) => {
      requiredUuid(item.id);
      requiredString(item.title);
      requiredString(item.created_at);
      requiredString(item.updated_at);
    });
  } else if (/^\/sessions\/[^/]+\/messages$/.test(path) && method === 'GET') {
    validateItems(payload, (item) => {
      requiredUuid(item.id);
      if (!['user', 'assistant', 'system'].includes(requiredString(item.role))) {
        throw new Error('invalid upstream response');
      }
      if (typeof item.content !== 'string') throw new Error('invalid upstream response');
      record(item.meta);
      requiredString(item.created_at);
    });
  } else if (path === '/collections' && method === 'GET') {
    validateItems(payload, (item) => {
      requiredUuid(item.id);
      requiredString(item.name);
      if (item.description !== undefined && item.description !== null) {
        requiredString(item.description);
      }
    });
  } else if (path === '/documents' && method === 'GET') {
    validateItems(payload, (item) => {
      requiredUuid(item.id);
      requiredString(item.title);
      requiredString(item.source_filename);
      requiredString(item.mime);
      array(item.collection_ids).forEach(requiredUuid);
      for (const rawVersion of array(item.versions)) {
        const version = record(rawVersion);
        requiredUuid(version.id);
        positiveInteger(version.version);
        requiredString(version.status);
        nonNegativeInteger(version.page_count);
        nonNegativeInteger(version.chunk_count);
        nonNegativeInteger(version.file_size_bytes);
      }
    });
  } else if (/^\/messages\/[^/]+\/citations\/S[1-9][0-9]*\/preview$/.test(path)) {
    requiredUuid(payload.message_id);
    requiredString(payload.source_id);
    requiredString(payload.title);
    requiredString(payload.source_filename);
    positiveInteger(payload.page_start);
    positiveInteger(payload.page_end);
    requiredString(payload.text);
  }
  return payload;
}

export function record(value: unknown): JsonRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('invalid upstream response');
  }
  return value as JsonRecord;
}

export function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error('invalid upstream response');
  return value;
}

function requiredString(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error('invalid upstream response');
  }
  return value;
}

function requiredUuid(value: unknown): string {
  const result = requiredString(value);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result)) {
    throw new Error('invalid upstream response');
  }
  return result;
}

function positiveInteger(value: unknown): number {
  if (!Number.isInteger(value) || (value as number) < 1) {
    throw new Error('invalid upstream response');
  }
  return value as number;
}

function nonNegativeInteger(value: unknown): number {
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw new Error('invalid upstream response');
  }
  return value as number;
}

function validateItems(payload: JsonRecord, validator: (item: JsonRecord) => void): void {
  for (const raw of array(payload.items)) validator(record(raw));
  if (
    payload.next_cursor !== undefined &&
    payload.next_cursor !== null &&
    typeof payload.next_cursor !== 'string'
  ) {
    throw new Error('invalid upstream response');
  }
}
