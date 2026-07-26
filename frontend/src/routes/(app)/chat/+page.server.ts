import { apiJson } from '$lib/server/api';
import { allPages } from '$lib/server/pagination';
import type { CollectionItem, DocumentItem, ItemPage, MessageItem, SessionItem } from '$lib/types';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async (event) => {
  const [sessionPage, collectionPage, documentPage] = await Promise.all([
    allPages<SessionItem>(event, '/sessions'),
    apiJson<ItemPage<CollectionItem>>(event, '/collections'),
    apiJson<ItemPage<DocumentItem>>(event, '/documents')
  ]);
  const requested = event.url.searchParams.get('session');
  const selected = sessionPage.find((item) => item.id === requested) ?? sessionPage[0] ?? null;
  const messages = selected
    ? await allPages<MessageItem>(event, `/sessions/${selected.id}/messages`)
    : [];
  return {
    sessions: sessionPage,
    selected,
    messages,
    collections: collectionPage.items,
    documents: documentPage.items
  };
};
