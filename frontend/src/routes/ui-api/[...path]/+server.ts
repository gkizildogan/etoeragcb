import { json, type RequestHandler } from '@sveltejs/kit';
import { apiRaw, publicError } from '$lib/server/api';
import { validateApiPayload } from '$lib/server/validation';

const UUID = '[0-9a-fA-F-]{36}';
const ALLOWED: ReadonlyArray<[string, RegExp]> = [
  ['GET', /^me$/],
  ['GET', /^sessions$/],
  ['POST', /^sessions$/],
  ['DELETE', new RegExp(`^sessions/${UUID}$`)],
  ['GET', new RegExp(`^sessions/${UUID}/messages$`)],
  ['POST', /^chat$/],
  ['POST', new RegExp(`^messages/${UUID}/feedback$`)],
  ['GET', new RegExp(`^messages/${UUID}/citations/S[1-9][0-9]*/preview$`)],
  ['GET', /^documents$/],
  ['POST', /^documents$/],
  ['POST', new RegExp(`^documents/${UUID}/reindex$`)],
  ['DELETE', new RegExp(`^documents/${UUID}$`)],
  ['GET', /^collections$/],
  ['POST', /^collections$/],
  ['PATCH', new RegExp(`^collections/${UUID}$`)],
  ['DELETE', new RegExp(`^collections/${UUID}$`)],
  ['PUT', new RegExp(`^collections/${UUID}/documents/${UUID}$`)],
  ['DELETE', new RegExp(`^collections/${UUID}/documents/${UUID}$`)]
];

const handler: RequestHandler = async (event) => {
  const method = event.request.method;
  const path = event.params.path ?? '';
  if (!ALLOWED.some(([allowedMethod, pattern]) => allowedMethod === method && pattern.test(path))) {
    return json({ error: { code: 'not_found', message: 'Not found.' } }, { status: 404 });
  }

  const headers = new Headers();
  for (const name of ['content-type', 'idempotency-key']) {
    const value = event.request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = !['GET', 'HEAD'].includes(method);
  const body = hasBody ? await event.request.arrayBuffer() : undefined;
  const chat = path === 'chat';
  const upload = path === 'documents' && method === 'POST';

  try {
    const response = await apiRaw(
      event,
      `/${path}${event.url.search}`,
      {
        method,
        headers,
        body
      },
      chat ? 240_000 : upload ? 120_000 : 15_000
    );
    const responseHeaders = new Headers({
      'Cache-Control': chat ? 'no-cache, no-store' : 'private, no-store',
      'Content-Type': response.headers.get('content-type') ?? 'application/json'
    });
    if (chat) responseHeaders.set('X-Accel-Buffering', 'no');
    if (!chat && response.status !== 204) {
      const payload = validateApiPayload(`/${path}`, method, await response.json());
      return json(payload, { status: response.status, headers: responseHeaders });
    }
    return new Response(response.body, { status: response.status, headers: responseHeaders });
  } catch (error) {
    const mapped = publicError(error);
    return json(mapped.body, {
      status: mapped.status,
      headers: { 'Cache-Control': 'private, no-store' }
    });
  }
};

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
