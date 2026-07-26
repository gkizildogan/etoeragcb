import { json, type RequestHandler } from '@sveltejs/kit';
import { logoutUpstream } from '$lib/server/api';

export const POST: RequestHandler = async (event) => {
  await logoutUpstream(event);
  return json({ ok: true }, { headers: { 'Cache-Control': 'no-store' } });
};
