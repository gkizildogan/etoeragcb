<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { isAdministrator, type CollectionItem } from '$lib/types';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
  const administrator = $derived(isAdministrator(data.profile));
  let createOpen = $state(false);
  let newName = $state('');
  let newDescription = $state('');
  let error = $state('');

  async function createCollection(): Promise<void> {
    if (!newName.trim()) return;
    const response = await fetch('/ui-api/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: newName.trim(),
        description: newDescription.trim() || null
      })
    });
    if (!response.ok) {
      error = 'Could not create the collection.';
      return;
    }
    newName = '';
    newDescription = '';
    createOpen = false;
    await invalidateAll();
  }

  async function updateCollection(
    collection: CollectionItem,
    name: string,
    description: string
  ): Promise<void> {
    error = '';
    const response = await fetch(`/ui-api/collections/${collection.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), description: description.trim() || null })
    });
    if (!response.ok) {
      error = 'Could not update the collection.';
      return;
    }
    await invalidateAll();
  }

  async function updateMembership(collectionId: string, selected: string[]): Promise<void> {
    const current = new Set(
      data.documents
        .filter((document) => document.collection_ids.includes(collectionId))
        .map((document) => document.id)
    );
    const desired = new Set(selected);
    const operations = [
      ...[...desired]
        .filter((id) => !current.has(id))
        .map((id) =>
          fetch(`/ui-api/collections/${collectionId}/documents/${id}`, { method: 'PUT' })
        ),
      ...[...current]
        .filter((id) => !desired.has(id))
        .map((id) =>
          fetch(`/ui-api/collections/${collectionId}/documents/${id}`, { method: 'DELETE' })
        )
    ];
    const responses = await Promise.all(operations);
    if (responses.some((response) => !response.ok)) {
      error = 'Could not update document membership.';
      return;
    }
    await invalidateAll();
  }

  async function remove(collection: CollectionItem): Promise<void> {
    if (!confirm(`Delete “${collection.name}”? Documents will remain available.`)) return;
    const response = await fetch(`/ui-api/collections/${collection.id}`, { method: 'DELETE' });
    if (!response.ok) {
      error = 'Could not delete the collection.';
      return;
    }
    await invalidateAll();
  }
</script>

<svelte:head>
  <title>Collections · Knowledge Assistant</title>
</svelte:head>

<div class="page-shell">
  <header class="page-header">
    <div>
      <p class="eyebrow">Knowledge organization</p>
      <h1>Collections</h1>
      <p>Group related documents to create focused retrieval scopes.</p>
    </div>
    {#if administrator}
      <button class="primary" type="button" onclick={() => (createOpen = !createOpen)}>
        {createOpen ? 'Cancel' : 'New collection'}
      </button>
    {/if}
  </header>

  <div class="summary-row">
    <div class="summary panel">
      <span>{data.collections.length}</span>
      <p>Collections</p>
    </div>
    <div class="summary panel">
      <span>{data.documents.length}</span>
      <p>Documents</p>
    </div>
    <div class="summary panel">
      <span>{data.revision ?? '—'}</span>
      <p>Retrieval revision</p>
    </div>
  </div>

  {#if !administrator}
    <div class="alert info">
      You can inspect collections. An administrator manages their contents.
    </div>
  {/if}
  {#if error}<div class="alert error" role="alert">{error}</div>{/if}

  {#if administrator && createOpen}
    <section class="create-card panel">
      <div>
        <p class="eyebrow">New scope</p>
        <h2>Create a collection</h2>
      </div>
      <label>
        <span>Name</span>
        <input bind:value={newName} maxlength="200" placeholder="e.g. Product handbook" />
      </label>
      <label>
        <span>Description</span>
        <textarea bind:value={newDescription} maxlength="4000" rows="3"></textarea>
      </label>
      <button class="primary" type="button" disabled={!newName.trim()} onclick={createCollection}>
        Create collection
      </button>
    </section>
  {/if}

  {#if data.collections.length === 0}
    <div class="empty panel">
      <span aria-hidden="true">▦</span>
      <h2>No collections yet</h2>
      <p>Create a collection to group documents into a focused search scope.</p>
    </div>
  {:else}
    <div class="collection-list">
      {#each data.collections as collection}
        {@render CollectionCard({
          collection,
          documents: data.documents,
          administrator,
          onUpdate: updateCollection,
          onMembership: updateMembership,
          onRemove: remove
        })}
      {/each}
    </div>
  {/if}
</div>

{#snippet CollectionCard({
  collection,
  documents,
  administrator,
  onUpdate,
  onMembership,
  onRemove
}: {
  collection: CollectionItem;
  documents: PageData['documents'];
  administrator: boolean;
  onUpdate: (collection: CollectionItem, name: string, description: string) => Promise<void>;
  onMembership: (collectionId: string, selected: string[]) => Promise<void>;
  onRemove: (collection: CollectionItem) => Promise<void>;
})}
  {@const initialMembers = documents
    .filter((document) => document.collection_ids.includes(collection.id))
    .map((document) => document.id)}
  <article class="collection-card panel">
    <details>
      <summary>
        <span class="collection-icon" aria-hidden="true">▦</span>
        <span class="collection-title">
          <strong>{collection.name}</strong>
          <small
            >{initialMembers.length} {initialMembers.length === 1 ? 'document' : 'documents'}</small
          >
        </span>
        <span class="chevron" aria-hidden="true">⌄</span>
      </summary>
      <div class="collection-body">
        {#if administrator}
          <form
            class="details-form"
            onsubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              const rawName = form.get('name');
              const rawDescription = form.get('description');
              void onUpdate(
                collection,
                typeof rawName === 'string' ? rawName : '',
                typeof rawDescription === 'string' ? rawDescription : ''
              );
            }}
          >
            <label>
              <span>Name</span>
              <input name="name" value={collection.name} maxlength="200" required />
            </label>
            <label>
              <span>Description</span>
              <textarea name="description" rows="3" maxlength="4000"
                >{collection.description ?? ''}</textarea
              >
            </label>
            <button type="submit">Save details</button>
          </form>

          <form
            class="membership"
            onsubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              void onMembership(collection.id, form.getAll('documents').map(String));
            }}
          >
            <fieldset>
              <legend>Documents</legend>
              {#each documents as document}
                <label>
                  <input
                    type="checkbox"
                    name="documents"
                    value={document.id}
                    checked={initialMembers.includes(document.id)}
                  />
                  <span>{document.title}</span>
                </label>
              {/each}
              {#if documents.length === 0}<p>No documents available.</p>{/if}
            </fieldset>
            <button type="submit">Save document membership</button>
          </form>
          <div class="danger-zone">
            <div>
              <strong>Delete collection</strong>
              <p>Documents are not deleted.</p>
            </div>
            <button class="danger" type="button" onclick={() => onRemove(collection)}>Delete</button
            >
          </div>
        {:else}
          <p class="description">{collection.description || 'No description.'}</p>
          <div class="member-documents">
            {#each documents.filter((document) => initialMembers.includes(document.id)) as document}
              <span>{document.title}</span>
            {/each}
            {#if initialMembers.length === 0}<p>No documents in this collection.</p>{/if}
          </div>
        {/if}
      </div>
    </details>
  </article>
{/snippet}

<style>
  .page-shell {
    width: min(100%, 68rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3rem);
  }

  .page-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1.5rem;
  }

  h1 {
    margin: 0.25rem 0 0.4rem;
    font-size: clamp(2rem, 4vw, 3.2rem);
    letter-spacing: -0.06em;
  }

  .page-header p:last-child {
    margin: 0;
    color: var(--muted);
  }

  .summary-row {
    margin: 1.8rem 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.75rem;
  }

  .summary {
    padding: 1rem;
  }

  .summary span {
    color: #17483c;
    font-size: 1.65rem;
    font-weight: 780;
    letter-spacing: -0.04em;
  }

  .summary p {
    margin: 0.2rem 0 0;
    color: var(--muted);
    font-size: 0.7rem;
  }

  .create-card {
    padding: 1.2rem;
    margin: 1.2rem 0;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.9rem;
    box-shadow: var(--shadow);
  }

  .create-card h2 {
    margin: 0.25rem 0;
  }

  .create-card > div,
  .create-card label:last-of-type {
    grid-column: 1 / -1;
  }

  .create-card label {
    display: grid;
    gap: 0.35rem;
    font-size: 0.75rem;
    font-weight: 700;
  }

  .create-card button {
    width: fit-content;
  }

  .collection-list {
    display: grid;
    gap: 0.75rem;
  }

  .collection-card {
    overflow: hidden;
  }

  .collection-card summary {
    min-height: 4.4rem;
    padding: 0.9rem 1rem;
    display: grid;
    grid-template-columns: 2.4rem minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.8rem;
    cursor: pointer;
    list-style: none;
  }

  .collection-card summary::-webkit-details-marker {
    display: none;
  }

  .collection-icon {
    display: grid;
    width: 2.4rem;
    aspect-ratio: 1;
    place-items: center;
    color: var(--accent-strong);
    border-radius: 0.65rem;
    background: var(--accent-soft);
  }

  .collection-title {
    min-width: 0;
    display: grid;
  }

  .collection-title strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .collection-title small {
    margin-top: 0.15rem;
    color: var(--muted);
  }

  .chevron {
    color: var(--muted);
    font-size: 1.1rem;
  }

  details[open] .chevron {
    transform: rotate(180deg);
  }

  .collection-body {
    padding: 1rem;
    border-top: 1px solid var(--line);
    background: #fbfbf7;
  }

  .details-form {
    display: grid;
    grid-template-columns: 1fr 1.5fr auto;
    align-items: end;
    gap: 0.8rem;
  }

  .details-form label {
    display: grid;
    gap: 0.35rem;
    font-size: 0.72rem;
    font-weight: 700;
  }

  .details-form textarea {
    min-height: 2.75rem;
  }

  .membership {
    margin-top: 1rem;
    display: flex;
    align-items: flex-end;
    gap: 0.75rem;
  }

  fieldset {
    min-width: 0;
    padding: 0.7rem;
    flex: 1;
    display: flex;
    flex-wrap: wrap;
    gap: 0.65rem;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }

  legend {
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 700;
  }

  fieldset label {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.72rem;
  }

  fieldset input {
    width: 1rem;
    min-height: 1rem;
  }

  .danger-zone {
    margin-top: 1.1rem;
    padding-top: 0.9rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 1px solid #edd5d1;
  }

  .danger-zone strong {
    color: var(--danger);
    font-size: 0.75rem;
  }

  .danger-zone p {
    margin: 0.15rem 0 0;
    color: var(--muted);
    font-size: 0.67rem;
  }

  .description {
    margin-top: 0;
    color: var(--muted);
    line-height: 1.6;
  }

  .member-documents {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .member-documents span {
    padding: 0.35rem 0.55rem;
    border-radius: 1rem;
    background: var(--surface-soft);
    font-size: 0.7rem;
  }

  .empty {
    min-height: 22rem;
    margin-top: 1rem;
    display: grid;
    place-content: center;
    justify-items: center;
    text-align: center;
  }

  .empty > span {
    color: var(--accent-strong);
    font-size: 2rem;
  }

  .empty h2 {
    margin: 0.8rem 0 0.25rem;
  }

  .empty p {
    margin: 0;
    color: var(--muted);
  }

  @media (max-width: 760px) {
    .page-header {
      align-items: flex-start;
      flex-direction: column;
    }

    .page-header button {
      width: 100%;
    }

    .summary-row {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .create-card,
    .details-form {
      grid-template-columns: 1fr;
    }

    .create-card > *,
    .details-form > * {
      grid-column: auto;
    }

    .membership {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
