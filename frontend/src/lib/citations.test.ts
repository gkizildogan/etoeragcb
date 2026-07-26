import { describe, expect, it } from 'vitest';
import { safeExternalUrl, validateCitations } from './citations';

describe('citation validation', () => {
  it('keeps valid document and web citations while dropping unsafe targets', () => {
    const citations = validateCitations({
      '[S1]': {
        marker: '[S1]',
        source_id: 'S1',
        source_type: 'document',
        title: 'Handbook',
        document_id: '11111111-1111-4111-8111-111111111111',
        document_version_id: '22222222-2222-4222-8222-222222222222',
        page_start: 2,
        page_end: 3
      },
      '[S2]': {
        marker: '[S2]',
        source_id: 'S2',
        source_type: 'web',
        title: 'Unsafe',
        uri: 'javascript:alert(1)'
      }
    });
    expect(Object.keys(citations)).toEqual(['[S1]']);
  });

  it('allows only credential-free HTTP(S) URLs', () => {
    expect(safeExternalUrl('https://example.com/path')).toBe(true);
    expect(safeExternalUrl('https://user:secret@example.com')).toBe(false);
    expect(safeExternalUrl('data:text/html,hello')).toBe(false);
  });
});
