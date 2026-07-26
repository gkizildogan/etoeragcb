import type { RequestEvent } from '@sveltejs/kit';
import type { ItemPage } from '$lib/types';
import { apiJson, GatewayError } from './api';

export async function allPages<T>(event: RequestEvent, path: string, limit = 100): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < 100; page += 1) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set('cursor', cursor);
    const payload = await apiJson<ItemPage<T>>(event, `${path}?${query.toString()}`);
    if (!Array.isArray(payload.items)) throw new GatewayError('invalid_upstream_response', 502);
    items.push(...payload.items);
    if (payload.next_cursor == null) return items;
    if (typeof payload.next_cursor !== 'string' || !payload.next_cursor) {
      throw new GatewayError('invalid_upstream_response', 502);
    }
    cursor = payload.next_cursor;
  }
  throw new GatewayError('invalid_upstream_response', 502);
}
