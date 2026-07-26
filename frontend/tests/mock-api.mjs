import { createServer } from 'node:http';

const ids = {
  tenant: '11111111-1111-4111-8111-111111111111',
  session: '22222222-2222-4222-8222-222222222222',
  userMessage: '33333333-3333-4333-8333-333333333333',
  assistantMessage: '44444444-4444-4444-8444-444444444444',
  document: '55555555-5555-4555-8555-555555555555',
  version: '66666666-6666-4666-8666-666666666666',
  collection: '77777777-7777-4777-8777-777777777777',
  otherTenant: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
};

let sessions = [
  {
    id: ids.session,
    title: 'Product handbook',
    created_at: '2026-07-20T10:00:00Z',
    updated_at: '2026-07-26T10:00:00Z'
  }
];
const messages = [
  {
    id: ids.userMessage,
    role: 'user',
    content: 'What is the travel policy?',
    meta: {},
    created_at: '2026-07-26T10:00:00Z'
  },
  {
    id: ids.assistantMessage,
    role: 'assistant',
    content: 'Travel requires manager approval [S1].',
    meta: {
      citations: {
        '[S1]': {
          marker: '[S1]',
          source_id: 'S1',
          source_type: 'document',
          title: 'Employee handbook',
          document_id: ids.document,
          document_version_id: ids.version,
          source_filename: 'handbook.pdf',
          page_start: 2,
          page_end: 2
        }
      },
      retrieval: { web_status: 'not_requested' }
    },
    created_at: '2026-07-26T10:00:01Z'
  }
];
let collections = [
  {
    id: ids.collection,
    name: 'People operations',
    description: 'Policies and employee guidance'
  }
];
const profiles = new Map();
const documents = [
  {
    id: ids.document,
    title: 'Employee handbook',
    source_filename: 'handbook.pdf',
    mime: 'application/pdf',
    active_version_id: ids.version,
    collection_ids: [ids.collection],
    versions: [
      {
        id: ids.version,
        version: 1,
        status: 'active',
        page_count: 12,
        chunk_count: 24,
        file_size_bytes: 120000
      }
    ]
  }
];

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', 'http://127.0.0.1:4174');
  const path = url.pathname;
  if (request.method === 'POST' && path === '/api/auth/login') {
    const body = await jsonBody(request);
    const role = body.email === 'member@example.com' ? 'member' : 'administrator';
    const tenantId = body.tenant_id ?? ids.tenant;
    const accessToken = `mock-access-token-${role}-${tenantId}`;
    profiles.set(accessToken, { role, tenantId });
    return send(response, 200, {
      access_token: accessToken,
      refresh_token: 'mock-refresh-token',
      tenant_id: tenantId,
      expires_in: 900,
      refresh_expires_in: 3600
    });
  }
  if (request.method === 'POST' && path === '/api/auth/refresh') {
    return send(response, 200, {
      access_token: 'mock-access-token-rotated',
      refresh_token: 'mock-refresh-token-rotated',
      tenant_id: ids.tenant,
      expires_in: 900,
      refresh_expires_in: 3600
    });
  }
  if (request.method === 'POST' && path === '/api/auth/logout') return send(response, 204);
  if (request.method === 'GET' && path === '/api/me') {
    const accessToken = (request.headers.authorization ?? '').replace(/^Bearer /, '');
    const profile = profiles.get(accessToken) ?? {
      role: 'administrator',
      tenantId: ids.tenant
    };
    return send(response, 200, {
      email: profile.role === 'member' ? 'member@example.com' : 'admin@example.com',
      active_tenant_id: profile.tenantId,
      is_superuser: false,
      memberships: [
        {
          tenant_id: ids.tenant,
          slug: 'acme',
          name: 'Acme Knowledge',
          role: profile.role,
          active: true
        },
        {
          tenant_id: ids.otherTenant,
          slug: 'other',
          name: 'Other Knowledge',
          role: profile.role,
          active: true
        }
      ]
    });
  }
  if (request.method === 'GET' && path === '/api/sessions') {
    return send(response, 200, { items: sessions, next_cursor: null });
  }
  if (request.method === 'POST' && path === '/api/sessions') {
    const body = await jsonBody(request);
    const created = {
      id: '88888888-8888-4888-8888-888888888888',
      title: body.title ?? 'New conversation',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    };
    sessions = [created, ...sessions.filter((item) => item.id !== created.id)];
    return send(response, 201, created);
  }
  if (request.method === 'DELETE' && /^\/api\/sessions\/[^/]+$/.test(path)) {
    sessions = sessions.filter((item) => !path.endsWith(item.id));
    return send(response, 204);
  }
  if (request.method === 'GET' && /^\/api\/sessions\/[^/]+\/messages$/.test(path)) {
    return send(response, 200, { items: messages, next_cursor: null });
  }
  if (request.method === 'POST' && path === '/api/chat') {
    const body = await jsonBody(request);
    const messageId = '99999999-9999-4999-8999-999999999999';
    const createdAt = new Date().toISOString();
    const citations = messages[1].meta.citations;
    messages.push(
      {
        id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
        role: 'user',
        content: body.message,
        meta: {},
        created_at: createdAt
      },
      {
        id: messageId,
        role: 'assistant',
        content: 'The authoritative mock answer [S1].',
        meta: {
          citations,
          retrieval: { web_status: 'not_requested' }
        },
        created_at: createdAt
      }
    );
    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-store',
      'X-Accel-Buffering': 'no'
    });
    const events = [
      ['start', { request_id: body.client_request_id }],
      ['status', { stage: 'retrieving' }],
      ['delta', { text: 'The mock answer ' }],
      ['replace', { text: 'The authoritative mock answer [S1].' }],
      ['citations', { items: citations }],
      ['done', { message_id: messageId, route: 'answer', usage: {} }]
    ];
    for (const [index, [name, data]] of events.entries()) {
      response.write(`id: ${index + 1}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
    }
    response.end();
    return;
  }
  if (request.method === 'POST' && /^\/api\/messages\/[^/]+\/feedback$/.test(path)) {
    return send(response, 200, {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      message_id: path.split('/')[3],
      rating: 1,
      comment: null,
      created_at: new Date().toISOString()
    });
  }
  if (request.method === 'GET' && /^\/api\/messages\/[^/]+\/citations\/S1\/preview$/.test(path)) {
    return send(response, 200, {
      message_id: path.split('/')[3],
      source_id: 'S1',
      title: 'Employee handbook',
      source_filename: 'handbook.pdf',
      page_start: 2,
      page_end: 2,
      text: 'Travel requires approval from the employee’s manager before booking.'
    });
  }
  if (request.method === 'GET' && path === '/api/documents') {
    return send(response, 200, { items: documents, next_cursor: null });
  }
  if (request.method === 'POST' && path === '/api/documents') {
    await consume(request);
    return send(response, 202, { document_id: ids.document, document_version_id: ids.version });
  }
  if (request.method === 'POST' && /^\/api\/documents\/[^/]+\/reindex$/.test(path)) {
    return send(response, 202, { document_id: ids.document, document_version_id: ids.version });
  }
  if (request.method === 'DELETE' && /^\/api\/documents\/[^/]+$/.test(path)) {
    return send(response, 200, { id: ids.document });
  }
  if (request.method === 'GET' && path === '/api/collections') {
    return send(response, 200, {
      items: collections,
      next_cursor: null,
      retrieval_revision: 4
    });
  }
  if (request.method === 'POST' && path === '/api/collections') {
    const body = await jsonBody(request);
    const created = {
      id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      name: body.name,
      description: body.description
    };
    collections = [...collections, created];
    return send(response, 201, created);
  }
  if (
    ['PATCH', 'DELETE'].includes(request.method ?? '') &&
    /^\/api\/collections\/[^/]+$/.test(path)
  ) {
    if (request.method === 'PATCH') {
      const body = await jsonBody(request);
      return send(response, 200, { id: path.split('/').at(-1), ...body });
    }
    return send(response, 200, { id: path.split('/').at(-1) });
  }
  if (
    ['PUT', 'DELETE'].includes(request.method ?? '') &&
    /^\/api\/collections\/[^/]+\/documents\/[^/]+$/.test(path)
  ) {
    return send(response, 200, { ok: true });
  }
  send(response, 404, { detail: 'Not found' });
});

server.listen(4174, '127.0.0.1');

function send(response, status, body) {
  response.statusCode = status;
  response.setHeader('Cache-Control', 'no-store');
  if (body === undefined) {
    response.end();
    return;
  }
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify(body));
}

async function jsonBody(request) {
  const value = await consume(request);
  return value ? JSON.parse(value) : {};
}

async function consume(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}
