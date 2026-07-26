import { apiJson } from '$lib/server/api';
import type { CollectionItem, DocumentItem, ItemPage } from '$lib/types';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async (event) => {
  const [collections, documents, parent] = await Promise.all([
    apiJson<ItemPage<CollectionItem>>(event, '/collections'),
    apiJson<ItemPage<DocumentItem>>(event, '/documents'),
    event.parent()
  ]);
  return {
    collections: collections.items,
    documents: documents.items,
    revision: collections.retrieval_revision,
    profile: parent.profile
  };
};
