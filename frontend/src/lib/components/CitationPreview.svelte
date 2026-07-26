<script lang="ts">
  import { pageLabel } from '$lib/citations';
  import type { Citation } from '$lib/types';

  let {
    messageId,
    citation
  }: {
    messageId: string;
    citation: Citation;
  } = $props();

  let dialog: HTMLDialogElement;
  let trigger: HTMLButtonElement;
  let loading = $state(false);
  let unavailable = $state(false);
  const titleId = $derived(`preview-title-${messageId}-${citation.source_id}`);
  let preview = $state<{
    title: string;
    source_filename: string;
    page_start: number;
    page_end: number;
    text: string;
  } | null>(null);

  async function open(): Promise<void> {
    loading = true;
    unavailable = false;
    preview = null;
    dialog.showModal();
    try {
      const response = await fetch(
        `/ui-api/messages/${encodeURIComponent(messageId)}/citations/${encodeURIComponent(citation.source_id)}/preview`
      );
      if (!response.ok) {
        unavailable = true;
        return;
      }
      preview = (await response.json()) as typeof preview;
    } catch {
      unavailable = true;
    } finally {
      loading = false;
    }
  }

  function close(): void {
    dialog.close();
  }

  function restoreFocus(): void {
    trigger.focus();
  }

  function backdrop(event: MouseEvent): void {
    const rect = dialog.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    ) {
      close();
    }
  }
</script>

<button class="preview-button" bind:this={trigger} type="button" onclick={open}>Preview</button>

<dialog
  bind:this={dialog}
  onclose={restoreFocus}
  onclick={backdrop}
  onkeydown={(event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  }}
  aria-labelledby={titleId}
>
  <div class="dialog-card">
    <header>
      <div>
        <p class="eyebrow">Citation {citation.marker}</p>
        <h2 id={titleId}>{preview?.title ?? citation.title}</h2>
        <p>
          {preview?.source_filename ?? citation.source_filename ?? 'Document'}
          ·
          {preview
            ? preview.page_start === preview.page_end
              ? `Page ${preview.page_start}`
              : `Pages ${preview.page_start}–${preview.page_end}`
            : pageLabel(citation)}
        </p>
      </div>
      <button class="close ghost" type="button" aria-label="Close preview" onclick={close}>×</button
      >
    </header>
    <div class="preview-content" aria-live="polite">
      {#if loading}
        <p class="loading">Loading source text…</p>
      {:else if unavailable}
        <div class="alert info">Preview is no longer available.</div>
      {:else if preview}
        <pre>{preview.text}</pre>
      {/if}
    </div>
    <footer>
      <button type="button" onclick={close}>Close</button>
    </footer>
  </div>
</dialog>

<style>
  .preview-button {
    min-height: 2rem;
    padding: 0.35rem 0.65rem;
    color: var(--accent-strong);
    font-size: 0.75rem;
  }

  dialog {
    width: min(46rem, calc(100vw - 2rem));
    max-height: min(44rem, calc(100vh - 2rem));
    padding: 0;
    color: var(--ink);
    border: 0;
    border-radius: var(--radius-lg);
    background: var(--surface-raised);
    box-shadow: 0 28px 90px rgba(8, 22, 26, 0.35);
  }

  dialog::backdrop {
    background: rgba(12, 25, 29, 0.58);
    backdrop-filter: blur(3px);
  }

  .dialog-card {
    max-height: min(44rem, calc(100vh - 2rem));
    display: grid;
    grid-template-rows: auto minmax(10rem, 1fr) auto;
  }

  header {
    padding: 1.35rem 1.5rem 1rem;
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line);
  }

  h2 {
    margin: 0.25rem 0;
    font-size: 1.3rem;
    letter-spacing: -0.025em;
  }

  header p:last-child {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .close {
    width: 2.3rem;
    min-height: 2.3rem;
    padding: 0;
    flex: 0 0 auto;
    font-size: 1.5rem;
  }

  .preview-content {
    min-height: 12rem;
    padding: 1.25rem 1.5rem;
    overflow: auto;
    background: #f7f7f3;
  }

  pre {
    margin: 0;
    color: #26343a;
    font-family: inherit;
    font-size: 0.92rem;
    line-height: 1.7;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .loading {
    color: var(--muted);
  }

  footer {
    padding: 0.8rem 1.5rem;
    display: flex;
    justify-content: flex-end;
    border-top: 1px solid var(--line);
  }
</style>
