import { describe, expect, it } from 'vitest';
import { openSession, sealSession, tokenBundle } from './session';

const KEY = Buffer.alloc(32, 7);
const TOKENS = {
  access_token: 'access-value',
  refresh_token: 'refresh-value',
  tenant_id: '11111111-1111-4111-8111-111111111111',
  expires_in: 900,
  refresh_expires_in: 3600
};

describe('encrypted session cookie', () => {
  it('round-trips a versioned token bundle without exposing token text', () => {
    const bundle = tokenBundle(TOKENS, 1_000);
    const sealed = sealSession(bundle, KEY);

    expect(sealed).not.toContain('access-value');
    expect(openSession(sealed, KEY, 2_000)).toEqual(bundle);
  });

  it('rejects tampering, the wrong key, and hard expiry', () => {
    const bundle = tokenBundle(TOKENS, 1_000);
    const sealed = sealSession(bundle, KEY);
    const parts = sealed.split('.');
    const ciphertext = parts[2] ?? '';
    parts[2] = `${ciphertext.startsWith('a') ? 'b' : 'a'}${ciphertext.slice(1)}`;

    expect(openSession(parts.join('.'), KEY, 2_000)).toBeNull();
    expect(openSession(sealed, Buffer.alloc(32, 8), 2_000)).toBeNull();
    expect(openSession(sealed, KEY, bundle.hardExpiresAt + 1)).toBeNull();
  });
});
