import { fail, redirect } from '@sveltejs/kit';
import { loginUpstream, publicError } from '$lib/server/api';
import { setSessionCookie } from '$lib/server/session';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = ({ locals }) => {
  if (locals.session) redirect(303, '/chat');
  return {};
};

export const actions: Actions = {
  default: async (event) => {
    const form = await event.request.formData();
    const rawEmail = form.get('email');
    const rawPassword = form.get('password');
    const email = typeof rawEmail === 'string' ? rawEmail.trim() : '';
    const password = typeof rawPassword === 'string' ? rawPassword : '';
    if (!email || !password) {
      return fail(400, { message: 'Enter your email and password.', email });
    }
    try {
      const bundle = await loginUpstream(email, password);
      event.locals.session = bundle;
      setSessionCookie(event.cookies, bundle);
    } catch (error) {
      const mapped = publicError(error);
      return fail(mapped.status, { message: mapped.body.error.message, email });
    }
    redirect(303, '/chat');
  }
};
