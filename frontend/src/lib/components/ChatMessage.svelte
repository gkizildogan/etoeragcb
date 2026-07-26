<script lang="ts">
  import CitationPreview from './CitationPreview.svelte';
  import Markdown from './Markdown.svelte';
  import { pageLabel, validateCitations } from '$lib/citations';
  import type { MessageItem } from '$lib/types';

  let { message, streaming = false }: { message: MessageItem; streaming?: boolean } = $props();
  let comment = $state('');
  let feedbackState = $state<'idle' | 'sending' | 'saved' | 'error'>('idle');
  const citations = $derived(validateCitations(message.meta?.citations));

  async function feedback(rating: -1 | 1): Promise<void> {
    feedbackState = 'sending';
    const response = await fetch(`/ui-api/messages/${message.id}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating, comment: comment.trim() || null })
    });
    feedbackState = response.ok ? 'saved' : 'error';
  }
</script>

<article class:user={message.role === 'user'} class:assistant={message.role === 'assistant'}>
  <div class="avatar" aria-hidden="true">{message.role === 'user' ? 'Y' : 'K'}</div>
  <div class="message-body">
    <p class="speaker">{message.role === 'user' ? 'You' : 'Knowledge Assistant'}</p>
    <Markdown content={message.content} />

    {#if message.role === 'assistant'}
      {#if message.meta?.retrieval?.web_status === 'failed'}
        <div class="alert warning">Web search failed; available document evidence was used.</div>
      {:else if message.meta?.retrieval?.web_status === 'partial'}
        <div class="alert warning">Some web sources were unavailable.</div>
      {:else if message.meta?.retrieval?.web_status === 'empty'}
        <div class="alert info">Web search found no usable pages.</div>
      {/if}

      {#if Object.keys(citations).length > 0}
        <section class="sources" aria-label="Sources">
          <p>Sources</p>
          {#each Object.entries(citations) as [marker, citation]}
            <div class="source">
              <span class="marker">{marker}</span>
              <span class="source-copy">
                <strong>{citation.title}</strong>
                {#if pageLabel(citation)}<small>{pageLabel(citation)}</small>{/if}
              </span>
              {#if citation.source_type === 'web' && citation.uri}
                <a
                  class="source-action"
                  href={citation.uri}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Visit
                </a>
              {:else}
                <CitationPreview messageId={message.id} {citation} />
              {/if}
            </div>
          {/each}
        </section>
      {/if}

      {#if !streaming && message.id}
        <details class="feedback">
          <summary>{feedbackState === 'saved' ? 'Feedback saved' : 'Rate this answer'}</summary>
          <div>
            <input
              bind:value={comment}
              maxlength="2000"
              aria-label="Optional feedback"
              placeholder="Optional note"
            />
            <button
              type="button"
              aria-label="Helpful"
              disabled={feedbackState === 'sending'}
              onclick={() => feedback(1)}>Helpful</button
            >
            <button
              type="button"
              aria-label="Not helpful"
              disabled={feedbackState === 'sending'}
              onclick={() => feedback(-1)}>Not helpful</button
            >
          </div>
          {#if feedbackState === 'error'}
            <p class="feedback-error" role="alert">Could not save feedback.</p>
          {/if}
        </details>
      {/if}
    {/if}
  </div>
</article>

<style>
  article {
    padding: 1.2rem 0;
    display: grid;
    grid-template-columns: 2.15rem minmax(0, 1fr);
    gap: 0.85rem;
    border-bottom: 1px solid var(--line);
  }

  article:last-child {
    border-bottom: 0;
  }

  .avatar {
    display: grid;
    width: 2.15rem;
    aspect-ratio: 1;
    place-items: center;
    border-radius: 0.65rem;
    color: #15352f;
    background: var(--accent-soft);
    font-size: 0.75rem;
    font-weight: 850;
  }

  article.user .avatar {
    color: #425057;
    background: #e2e4df;
  }

  .speaker {
    margin: 0 0 0.42rem;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 780;
    letter-spacing: 0.02em;
  }

  .sources {
    margin-top: 1rem;
    padding-top: 0.85rem;
    border-top: 1px solid var(--line);
  }

  .sources > p {
    margin: 0 0 0.55rem;
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .source {
    min-height: 3rem;
    padding: 0.45rem 0.55rem;
    margin: 0.35rem 0;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    background: #fafaf6;
  }

  .marker {
    color: var(--accent-strong);
    font-size: 0.72rem;
    font-weight: 820;
  }

  .source-copy {
    min-width: 0;
    display: grid;
    margin-right: auto;
  }

  .source-copy strong {
    overflow: hidden;
    font-size: 0.78rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-copy small {
    color: var(--muted);
    font-size: 0.68rem;
  }

  .source-action {
    padding: 0.4rem 0.55rem;
    color: var(--accent-strong);
    font-size: 0.75rem;
    font-weight: 700;
  }

  .feedback {
    margin-top: 0.65rem;
    color: var(--muted);
    font-size: 0.72rem;
  }

  .feedback summary {
    cursor: pointer;
    width: fit-content;
  }

  .feedback > div {
    margin-top: 0.55rem;
    display: grid;
    grid-template-columns: minmax(8rem, 1fr) auto auto;
    gap: 0.4rem;
  }

  .feedback input,
  .feedback button {
    min-height: 2.2rem;
    font-size: 0.7rem;
  }

  .feedback-error {
    color: var(--danger);
  }

  @media (max-width: 560px) {
    article {
      grid-template-columns: 1.8rem minmax(0, 1fr);
      gap: 0.65rem;
    }

    .avatar {
      width: 1.8rem;
      border-radius: 0.5rem;
    }

    .feedback > div {
      grid-template-columns: 1fr 1fr;
    }

    .feedback input {
      grid-column: 1 / -1;
    }
  }
</style>
