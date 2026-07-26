import type { Handle } from '@sveltejs/kit';
import { openSession, SESSION_COOKIE } from '$lib/server/session';

export const handle: Handle = async ({ event, resolve }) => {
  const raw = event.cookies.get(SESSION_COOKIE);
  event.locals.session = openSession(raw);
  if (raw && !event.locals.session) {
    event.cookies.delete(SESSION_COOKIE, {
      path: '/',
      httpOnly: true,
      sameSite: 'lax',
      secure: true
    });
  }
  return resolve(event);
};
