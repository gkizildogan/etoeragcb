import { json, type RequestHandler } from '@sveltejs/kit';
import { apiJson, loginUpstream, logoutUpstream, publicError } from '$lib/server/api';
import { setSessionCookie } from '$lib/server/session';

export const POST: RequestHandler = async (event) => {
  try {
    const body = (await event.request.json()) as unknown;
    if (!isRecord(body)) return json({ error: { message: 'Invalid request.' } }, { status: 400 });
    const tenantId = typeof body.tenant_id === 'string' ? body.tenant_id : '';
    const password = typeof body.password === 'string' ? body.password : '';
    if (!UUID_RE.test(tenantId) || !password || password.length > 1024) {
      return json({ error: { message: 'Invalid request.' } }, { status: 400 });
    }
    const profile = await apiJson<{ email: string }>(event, '/me');
    const replacement = await loginUpstream(profile.email, password, tenantId);
    await logoutUpstream(event);
    event.locals.session = replacement;
    setSessionCookie(event.cookies, replacement);
    return json({ ok: true });
  } catch (error) {
    const mapped = publicError(error);
    return json(mapped.body, { status: mapped.status });
  }
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
