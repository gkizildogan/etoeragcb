<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { isAdministrator } from '$lib/types';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
  const administrator = $derived(isAdministrator(data.profile));
  let polling = $state(false);
  let uploadOpen = $state(false);
  let target = $state('new');
  let title = $state('');
  let collectionIds = $state<string[]>([]);
  let file = $state<File | null>(null);
  let uploadState = $state<'idle' | 'uploading' | 'success' | 'error'>('idle');
  let actionError = $state('');

  $effect(() => {
    if (!polling) return;
    const interval = window.setInterval(() => void invalidateAll(), 5000);
    return () => window.clearInterval(interval);
  });

  function selectTarget(value: string): void {
    target = value;
    const document = data.documents.find((item) => item.id === value);
    title = document?.title ?? '';
    collectionIds = document?.collection_ids ? [...document.collection_ids] : [];
  }

  async function upload(): Promise<void> {
    if (!file || !title.trim()) return;
    uploadState = 'uploading';
    const form = new FormData();
    form.set('file', file);
    form.set('title', title.trim());
    form.set('collection_ids_json', JSON.stringify(collectionIds));
    if (target !== 'new') form.set('document_id', target);
    const response = await fetch('/ui-api/documents', {
      method: 'POST',
      headers: { 'Idempotency-Key': `upload-${crypto.randomUUID()}` },
      body: form
    });
    if (!response.ok) {
      uploadState = 'error';
      return;
    }
    uploadState = 'success';
    polling = true;
    file = null;
    await invalidateAll();
  }

  async function reindex(documentId: string): Promise<void> {
    actionError = '';
    const response = await fetch(`/ui-api/documents/${documentId}/reindex`, {
      method: 'POST',
      headers: { 'Idempotency-Key': `reindex-${crypto.randomUUID()}` }
    });
    if (!response.ok) {
      actionError = 'Could not start reindexing.';
      return;
    }
    polling = true;
    await invalidateAll();
  }

  async function remove(documentId: string, name: string): Promise<void> {
    if (!confirm(`Delete “${name}”? Older citation previews will become unavailable.`)) return;
    actionError = '';
    const response = await fetch(`/ui-api/documents/${documentId}`, { method: 'DELETE' });
    if (!response.ok) {
      actionError = 'Could not delete the document.';
      return;
    }
    await invalidateAll();
  }

  function fileSize(value: number): string {
    if (!Number.isFinite(value) || value < 0) return 'Unknown size';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function statusClass(status: string): string {
    if (status === 'failed') return 'failed';
    if (['active', 'superseded'].includes(status)) return 'complete';
    return 'processing';
  }
</script>

<svelte:head>
  <title>Documents · Knowledge Assistant</title>
</svelte:head>

<div class="page-shell">
  <header class="page-header">
    <div>
      <p class="eyebrow">Knowledge library</p>
      <h1>Documents</h1>
      <p>Manage the files that ground your organization’s answers.</p>
    </div>
    {#if administrator}
      <button class="primary" type="button" onclick={() => (uploadOpen = !uploadOpen)}>
        {uploadOpen ? 'Close upload' : 'Upload document'}
      </button>
    {/if}
  </header>

  {#if !administrator}
    <div class="alert info">
      You can inspect documents and ingestion status. An administrator manages files.
    </div>
  {/if}
  {#if actionError}<div class="alert error" role="alert">{actionError}</div>{/if}

  {#if administrator && uploadOpen}
    <section class="upload panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Add knowledge</p>
          <h2>Upload a document or version</h2>
        </div>
        <span>PDF · TXT · MD · JSONL · DOCX</span>
      </div>
      <div class="upload-grid">
        <label>
          <span>Upload type</span>
          <select value={target} onchange={(event) => selectTarget(event.currentTarget.value)}>
            <option value="new">New document</option>
            {#each data.documents as document}
              <option value={document.id}>New version of {document.title}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>Title</span>
          <input bind:value={title} maxlength="300" placeholder="Document title" />
        </label>
        {#if target === 'new'}
          <fieldset>
            <legend>Collections</legend>
            {#each data.collections as collection}
              <label class="check">
                <input type="checkbox" value={collection.id} bind:group={collectionIds} />
                <span>{collection.name}</span>
              </label>
            {/each}
            {#if data.collections.length === 0}<p>No collections available.</p>{/if}
          </fieldset>
        {/if}
        <label class="file-drop">
          <span>{file ? file.name : 'Choose a file up to 50 MB'}</span>
          <input
            type="file"
            accept=".pdf,.txt,.md,.jsonl,.docx"
            onchange={(event) => (file = event.currentTarget.files?.[0] ?? null)}
          />
        </label>
      </div>
      {#if uploadState === 'error'}
        <div class="alert error" role="alert">The upload could not be accepted.</div>
      {:else if uploadState === 'success'}
        <div class="alert info">Upload accepted. Ingestion has started.</div>
      {/if}
      <div class="upload-actions">
        <button
          class="primary"
          type="button"
          disabled={!file || !title.trim() || uploadState === 'uploading'}
          onclick={upload}
        >
          {uploadState === 'uploading' ? 'Uploading…' : 'Start upload'}
        </button>
      </div>
    </section>
  {/if}

  <div class="inventory-bar">
    <div>
      <h2>Library</h2>
      <span>{data.documents.length} {data.documents.length === 1 ? 'document' : 'documents'}</span>
    </div>
    <label class="poll-toggle">
      <input type="checkbox" bind:checked={polling} />
      <span>Auto-refresh processing status</span>
    </label>
  </div>

  {#if data.documents.length === 0}
    <div class="empty panel">
      <span aria-hidden="true">▤</span>
      <h2>No documents yet</h2>
      <p>Uploaded documents and their ingestion progress will appear here.</p>
    </div>
  {:else}
    <div class="document-grid">
      {#each data.documents as document}
        <article class="document-card panel">
          <header>
            <span class="file-icon" aria-hidden="true"
              >{document.source_filename.split('.').at(-1)?.toUpperCase() ?? 'FILE'}</span
            >
            <div>
              <h2>{document.title}</h2>
              <p title={document.source_filename}>{document.source_filename}</p>
            </div>
          </header>
          <div class="versions">
            {#each document.versions as version}
              <div class="version">
                <span class:active={version.id === document.active_version_id} class="timeline"
                ></span>
                <div>
                  <div class="version-title">
                    <strong>Version {version.version}</strong>
                    <span class={statusClass(version.status)}>{version.status}</span>
                    {#if version.id === document.active_version_id}<small>Active</small>{/if}
                  </div>
                  <p>
                    {version.page_count} pages · {version.chunk_count} chunks ·
                    {fileSize(version.file_size_bytes)}
                  </p>
                  {#if version.status === 'failed'}
                    <p class="failure">
                      Ingestion failed: {version.error_code ?? 'ingestion_failed'}
                    </p>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          {#if administrator}
            <footer>
              <button
                type="button"
                disabled={!document.active_version_id}
                onclick={() => reindex(document.id)}>Reindex active</button
              >
              <button
                class="danger ghost"
                type="button"
                onclick={() => remove(document.id, document.title)}>Delete</button
              >
            </footer>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .page-shell {
    width: min(100%, 72rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3rem);
  }

  .page-header {
    margin-bottom: 1.6rem;
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

  .upload {
    padding: 1.2rem;
    margin: 1.5rem 0;
    box-shadow: var(--shadow);
  }

  .section-heading,
  .inventory-bar,
  .version-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .section-heading h2,
  .inventory-bar h2 {
    margin: 0.25rem 0 0;
    letter-spacing: -0.035em;
  }

  .section-heading > span,
  .inventory-bar span {
    color: var(--muted);
    font-size: 0.73rem;
  }

  .upload-grid {
    margin-top: 1.1rem;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.9rem;
  }

  .upload-grid > label {
    display: grid;
    gap: 0.4rem;
    font-size: 0.78rem;
    font-weight: 700;
  }

  fieldset {
    padding: 0.7rem;
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    gap: 0.8rem;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }

  legend {
    padding: 0 0.3rem;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 700;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.75rem;
  }

  .check input,
  .poll-toggle input {
    width: 1rem;
    min-height: 1rem;
  }

  .file-drop {
    min-height: 5rem;
    padding: 1rem;
    grid-column: 1 / -1;
    place-items: center;
    border: 1px dashed #9aaba5;
    border-radius: var(--radius-sm);
    color: var(--accent-strong);
    background: #f6faf7;
    cursor: pointer;
  }

  .file-drop input {
    width: auto;
    min-height: auto;
    font-size: 0.75rem;
  }

  .upload-actions {
    margin-top: 0.8rem;
    display: flex;
    justify-content: flex-end;
  }

  .inventory-bar {
    margin: 2.2rem 0 1rem;
  }

  .inventory-bar > div {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
  }

  .poll-toggle {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--muted);
    font-size: 0.73rem;
  }

  .document-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }

  .document-card {
    padding: 1.1rem;
    display: flex;
    flex-direction: column;
  }

  .document-card > header {
    display: grid;
    grid-template-columns: 2.7rem minmax(0, 1fr);
    gap: 0.75rem;
  }

  .file-icon {
    display: grid;
    width: 2.7rem;
    height: 3.25rem;
    place-items: end center;
    padding-bottom: 0.42rem;
    color: #2f6757;
    border-radius: 0.4rem 0.4rem 0.65rem 0.4rem;
    background: var(--accent-soft);
    font-size: 0.55rem;
    font-weight: 850;
  }

  .document-card h2 {
    margin: 0.2rem 0;
    overflow: hidden;
    font-size: 1rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .document-card header p {
    margin: 0;
    overflow: hidden;
    color: var(--muted);
    font-size: 0.7rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .versions {
    margin: 1rem 0;
  }

  .version {
    display: grid;
    grid-template-columns: 0.7rem minmax(0, 1fr);
    gap: 0.55rem;
  }

  .version + .version {
    margin-top: 0.75rem;
  }

  .timeline {
    width: 0.55rem;
    height: 0.55rem;
    margin-top: 0.3rem;
    border: 2px solid #9aa49f;
    border-radius: 50%;
  }

  .timeline.active {
    border-color: var(--accent-strong);
    background: var(--accent);
  }

  .version-title {
    justify-content: flex-start;
  }

  .version-title strong {
    font-size: 0.76rem;
  }

  .version-title span,
  .version-title small {
    padding: 0.15rem 0.35rem;
    border-radius: 1rem;
    font-size: 0.58rem;
    font-weight: 760;
    text-transform: capitalize;
  }

  .version-title span.complete {
    color: #24604f;
    background: var(--accent-soft);
  }

  .version-title span.processing {
    color: #73511c;
    background: var(--warning-soft);
  }

  .version-title span.failed {
    color: var(--danger);
    background: var(--danger-soft);
  }

  .version-title small {
    color: var(--muted);
    background: var(--surface-soft);
  }

  .version p {
    margin: 0.25rem 0 0;
    color: var(--muted);
    font-size: 0.68rem;
  }

  .version p.failure {
    color: var(--danger);
  }

  .document-card footer {
    margin-top: auto;
    padding-top: 0.8rem;
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    border-top: 1px solid var(--line);
  }

  .document-card footer button {
    min-height: 2.2rem;
    font-size: 0.68rem;
  }

  .empty {
    min-height: 22rem;
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

  @media (max-width: 950px) {
    .document-grid {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 600px) {
    .page-header {
      align-items: flex-start;
      flex-direction: column;
    }

    .page-header button {
      width: 100%;
    }

    .upload-grid {
      grid-template-columns: 1fr;
    }

    .upload-grid > * {
      grid-column: auto;
    }

    .inventory-bar {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
