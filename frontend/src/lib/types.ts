export interface Membership {
  tenant_id: string;
  name?: string;
  slug?: string;
  role: string;
  active: boolean;
}

export interface Profile {
  email: string;
  active_tenant_id: string;
  is_superuser?: boolean;
  memberships: Membership[];
}

export interface SessionItem {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  marker: string;
  source_id: string;
  source_type: 'document' | 'web';
  title: string;
  document_id?: string;
  document_version_id?: string;
  source_filename?: string;
  page_start?: number;
  page_end?: number;
  uri?: string;
}

export interface MessageItem {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  meta: {
    citations?: Record<string, unknown>;
    retrieval?: {
      web_status?: string;
    };
  };
  created_at: string;
}

export interface CollectionItem {
  id: string;
  name: string;
  description?: string | null;
}

export interface DocumentVersion {
  id: string;
  version: number;
  status: string;
  page_count: number;
  chunk_count: number;
  file_size_bytes: number;
  error_code?: string | null;
}

export interface DocumentItem {
  id: string;
  title: string;
  source_filename: string;
  mime: string;
  active_version_id?: string | null;
  collection_ids: string[];
  versions: DocumentVersion[];
}

export interface ItemPage<T> {
  items: T[];
  next_cursor?: string | null;
  retrieval_revision?: number;
}

export function activeRole(profile: Profile): string {
  return (
    profile.memberships.find((item) => item.tenant_id === profile.active_tenant_id)?.role ??
    'member'
  );
}

export function isAdministrator(profile: Profile): boolean {
  return profile.is_superuser === true || ['admin', 'administrator'].includes(activeRole(profile));
}
