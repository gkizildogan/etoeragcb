import { redirect } from '@sveltejs/kit';
import { apiJson, GatewayError } from '$lib/server/api';
import type { Profile } from '$lib/types';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = async (event) => {
  if (!event.locals.session) redirect(303, '/login');
  try {
    const profile = await apiJson<Profile>(event, '/me');
    return { profile, tenantId: event.locals.session.tenantId };
  } catch (error) {
    if (error instanceof GatewayError && error.status === 401) redirect(303, '/login');
    throw error;
  }
};
