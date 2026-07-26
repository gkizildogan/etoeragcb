import type { Citation } from './types';

const MARKER = /^\[S([1-9][0-9]*)\]$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function validateCitations(value: unknown): Record<string, Citation> {
  if (!isRecord(value)) return {};
  const result: Record<string, Citation> = {};
  for (const [marker, raw] of Object.entries(value)) {
    if (!isRecord(raw)) continue;
    const match = MARKER.exec(marker);
    if (!match) continue;
    const sourceId = `S${match[1]}`;
    if (
      raw.marker !== marker ||
      raw.source_id !== sourceId ||
      (raw.source_type !== 'document' && raw.source_type !== 'web') ||
      typeof raw.title !== 'string' ||
      raw.title.length === 0
    ) {
      continue;
    }
    if (
      raw.source_type === 'document' &&
      (!isUuid(raw.document_id) || !isUuid(raw.document_version_id))
    ) {
      continue;
    }
    if (raw.source_type === 'web' && !safeExternalUrl(raw.uri)) continue;
    if (!validPage(raw.page_start) || !validPage(raw.page_end)) continue;
    if (
      typeof raw.page_start === 'number' &&
      typeof raw.page_end === 'number' &&
      raw.page_end < raw.page_start
    ) {
      continue;
    }
    result[marker] = raw as unknown as Citation;
  }
  return result;
}

export function safeExternalUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  try {
    const parsed = new URL(value);
    return (
      ['http:', 'https:'].includes(parsed.protocol) &&
      Boolean(parsed.hostname) &&
      parsed.username === '' &&
      parsed.password === ''
    );
  } catch {
    return false;
  }
}

export function pageLabel(citation: Citation): string {
  if (typeof citation.page_start !== 'number') return '';
  if (typeof citation.page_end === 'number' && citation.page_end !== citation.page_start) {
    return `Pages ${citation.page_start}–${citation.page_end}`;
  }
  return `Page ${citation.page_start}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID.test(value);
}

function validPage(value: unknown): boolean {
  return (
    value === undefined || value === null || (Number.isInteger(value) && (value as number) >= 1)
  );
}
