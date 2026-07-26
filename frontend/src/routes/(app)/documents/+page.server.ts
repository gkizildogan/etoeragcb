import { apiJson } from '$lib/server/api';
import type { CollectionItem, DocumentItem, ItemPage } from '$lib/types';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async (event) => {
  const [documents, collections, parent] = await Promise.all([
    apiJson<ItemPage<DocumentItem>>(event, '/documents'),
    apiJson<ItemPage<CollectionItem>>(event, '/collections'),
    event.parent()
  ]);
  return {
    documents: documents.items,
    collections: collections.items,
    profile: parent.profile
  };
};
