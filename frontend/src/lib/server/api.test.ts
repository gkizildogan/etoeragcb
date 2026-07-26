import type { RequestEvent } from '@sveltejs/kit';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiJson, logoutUpstream } from './api';
import { tokenBundle } from './session';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('gateway refresh rotation', () => {
  it('deduplicates concurrent refreshes and rotates both encrypted sessions', async () => {
    let refreshCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url =
          typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        const authorization = new Headers(init?.headers).get('Authorization');
        if (url.endsWith('/auth/refresh')) {
          refreshCalls += 1;
          await Promise.resolve();
          return Response.json({
            access_token: 'new-access',
            refresh_token: 'new-refresh',
            tenant_id: '11111111-1111-4111-8111-111111111111',
            expires_in: 900,
            refresh_expires_in: 3600
          });
        }
        if (authorization === 'Bearer old-access') return new Response(null, { status: 401 });
        return Response.json({ items: [], next_cursor: null });
      })
    );

    const first = event('old-refresh-a');
    const second = event('old-refresh-a');
    await Promise.all([apiJson(first, '/collections'), apiJson(second, '/collections')]);

    expect(refreshCalls).toBe(1);
    expect(first.locals.session?.accessToken).toBe('new-access');
    expect(second.locals.session?.accessToken).toBe('new-access');
    expect(first.cookies.set).toHaveBeenCalledOnce();
    expect(second.cookies.set).toHaveBeenCalledOnce();
  });

  it('clears local authentication even when upstream logout is unavailable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('offline')))
    );
    const requestEvent = event('logout-refresh');

    await logoutUpstream(requestEvent);

    expect(requestEvent.locals.session).toBeNull();
    expect(requestEvent.cookies.delete).toHaveBeenCalledOnce();
  });
});

function event(refreshToken: string): RequestEvent {
  return {
    locals: {
      session: tokenBundle({
        access_token: 'old-access',
        refresh_token: refreshToken,
        tenant_id: '11111111-1111-4111-8111-111111111111',
        expires_in: 900,
        refresh_expires_in: 3600
      })
    },
    cookies: {
      set: vi.fn(),
      delete: vi.fn()
    }
  } as unknown as RequestEvent;
}
