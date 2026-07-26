<script lang="ts">
  import { goto, invalidateAll } from '$app/navigation';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import { ChatAccumulator, SSEParser } from '$lib/sse';
  import type { Citation, MessageItem } from '$lib/types';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
  let title = $state('');
  let prompt = $state('');
  let collectionIds = $state<string[]>([]);
  let documentIds = $state<string[]>([]);
  let webSearch = $state(false);
  let sending = $state(false);
  let stage = $state('');
  let chatError = $state('');
  let streamedAnswer = $state('');
  let streamedCitations = $state<Record<string, Citation>>({});
  let localPrompt = $state('');
  let assistantId = $state('');

  async function createSession(): Promise<void> {
    const cleaned = title.trim();
    if (!cleaned) return;
    const response = await fetch('/ui-api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: cleaned })
    });
    if (!response.ok) return;
    const created = (await response.json()) as { id: string };
    title = '';
    await invalidateAll();
    await goto(`/chat?session=${created.id}`);
  }

  async function deleteSession(): Promise<void> {
    if (!data.selected || !confirm('Delete this conversation?')) return;
    const response = await fetch(`/ui-api/sessions/${data.selected.id}`, { method: 'DELETE' });
    if (response.ok) {
      await invalidateAll();
      await goto('/chat');
    }
  }

  async function send(): Promise<void> {
    const question = prompt.trim();
    if (!question || !data.selected || sending) return;
    const clientRequestId = crypto.randomUUID();
    const idempotencyKey = `chat-${clientRequestId}`;
    const body = JSON.stringify({
      session_id: data.selected.id,
      message: question,
      collection_ids: collectionIds,
      document_ids: documentIds,
      web_search: webSearch,
      client_request_id: clientRequestId
    });
    prompt = '';
    localPrompt = question;
    streamedAnswer = '';
    streamedCitations = {};
    assistantId = '';
    chatError = '';
    stage = 'Planning';
    sending = true;
    const accumulator = new ChatAccumulator();

    try {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const terminal = await consume(body, idempotencyKey, accumulator);
        if (terminal) break;
        if (attempt === 1) throw new Error('stream interrupted');
      }
      if (accumulator.errorCode) {
        chatError = `The answer could not be completed.${accumulator.retryable ? ' You can retry safely.' : ''}`;
      } else if (!accumulator.done) {
        chatError = 'The answer stream ended unexpectedly. You can retry safely.';
      } else {
        await invalidateAll();
      }
    } catch {
      chatError = 'The answer stream was interrupted. You can retry safely.';
    } finally {
      sending = false;
      stage = '';
      if (!chatError) {
        localPrompt = '';
        streamedAnswer = '';
      }
    }
  }

  async function consume(
    body: string,
    idempotencyKey: string,
    accumulator: ChatAccumulator
  ): Promise<boolean> {
    const response = await fetch('/ui-api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body
    });
    if (response.status === 425) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      return false;
    }
    if (!response.ok || !response.body) throw new Error('chat failed');
    const parser = new SSEParser();
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      const events = parser.feed(decoder.decode(value, { stream: !done }), done);
      for (const event of events) {
        accumulator.apply(event);
        streamedAnswer = accumulator.answer;
        streamedCitations = accumulator.citations;
        assistantId = accumulator.assistantMessageId ?? assistantId;
        if (accumulator.stages.length) {
          stage = `${accumulator.stages.at(-1)?.replace(/^\w/, (value) => value.toUpperCase())}…`;
        }
      }
      if (done || accumulator.done || accumulator.errorCode) {
        if (!done) await reader.cancel();
        return accumulator.done || accumulator.errorCode !== null;
      }
    }
  }

  function streamingMessage(): MessageItem {
    return {
      id: assistantId,
      role: 'assistant',
      content: streamedAnswer || (stage ? '' : 'Preparing an answer…'),
      meta: { citations: streamedCitations },
      created_at: new Date().toISOString()
    };
  }
</script>

<svelte:head>
  <title>Chat · Knowledge Assistant</title>
</svelte:head>

<div class="chat-layout">
  <aside class="sessions panel">
    <div class="sessions-heading">
      <div>
        <p class="eyebrow">Conversations</p>
        <h2>Your chats</h2>
      </div>
      <span>{data.sessions.length}</span>
    </div>
    <form
      class="new-session"
      onsubmit={(event) => {
        event.preventDefault();
        void createSession();
      }}
    >
      <input
        bind:value={title}
        maxlength="240"
        aria-label="Conversation title"
        placeholder="New conversation"
      />
      <button class="primary" type="submit" aria-label="Create conversation">+</button>
    </form>
    {#if data.sessions.length}
      <label class="mobile-session">
        <span class="sr-only">Current conversation</span>
        <select
          value={data.selected?.id}
          onchange={(event) => void goto(`/chat?session=${event.currentTarget.value}`)}
        >
          {#each data.sessions as session}
            <option value={session.id}>{session.title}</option>
          {/each}
        </select>
      </label>
    {/if}
    <nav aria-label="Conversations">
      {#each data.sessions as session}
        <a
          class:active={data.selected?.id === session.id}
          href={`/chat?session=${session.id}`}
          aria-current={data.selected?.id === session.id ? 'page' : undefined}
        >
          <span>{session.title}</span>
          <small>{new Date(session.updated_at).toLocaleDateString()}</small>
        </a>
      {/each}
    </nav>
  </aside>

  <section class="conversation">
    <header>
      <div>
        <p class="eyebrow">Private workspace</p>
        <h1>{data.selected?.title ?? 'Start a conversation'}</h1>
      </div>
      {#if data.selected}
        <button class="danger ghost" type="button" onclick={deleteSession}>Delete chat</button>
      {/if}
    </header>

    {#if data.selected}
      <details class="scope panel">
        <summary>
          <span>Knowledge scope</span>
          <small>
            {collectionIds.length + documentIds.length
              ? `${collectionIds.length + documentIds.length} selected`
              : 'All accessible sources'}
            {webSearch ? ' · Web on' : ''}
          </small>
        </summary>
        <div class="scope-grid">
          <fieldset>
            <legend>Collections</legend>
            {#if data.collections.length}
              {#each data.collections as collection}
                <label>
                  <input type="checkbox" value={collection.id} bind:group={collectionIds} />
                  <span>{collection.name}</span>
                </label>
              {/each}
            {:else}
              <p>No collections available.</p>
            {/if}
          </fieldset>
          <fieldset>
            <legend>Documents</legend>
            {#if data.documents.length}
              {#each data.documents as document}
                <label>
                  <input type="checkbox" value={document.id} bind:group={documentIds} />
                  <span>{document.title}</span>
                </label>
              {/each}
            {:else}
              <p>No documents available.</p>
            {/if}
          </fieldset>
          <label class="web-toggle">
            <input type="checkbox" bind:checked={webSearch} />
            <span
              ><strong>Search the web too</strong><small>Use current external sources</small></span
            >
          </label>
        </div>
      </details>

      <div class="messages" aria-live="polite">
        {#if data.messages.length === 0 && !localPrompt}
          <div class="empty-state">
            <span aria-hidden="true">✦</span>
            <h2>Ask your knowledge base</h2>
            <p>
              Choose a scope above or search everything you can access. Answers will include
              inspectable citations.
            </p>
          </div>
        {/if}
        {#each data.messages.filter( (message) => ['user', 'assistant'].includes(message.role) ) as message}
          <ChatMessage {message} />
        {/each}
        {#if localPrompt}
          <ChatMessage
            message={{
              id: '',
              role: 'user',
              content: localPrompt,
              meta: {},
              created_at: new Date().toISOString()
            }}
            streaming
          />
          <ChatMessage message={streamingMessage()} streaming />
          {#if stage}<p class="stage">{stage}</p>{/if}
        {/if}
        {#if chatError}<div class="alert error" role="alert">{chatError}</div>{/if}
      </div>

      <form
        class="composer panel"
        onsubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          bind:value={prompt}
          rows="2"
          maxlength="4000"
          placeholder="Ask about your knowledge sources…"
          aria-label="Message"
          onkeydown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}></textarea>
        <button class="primary send" type="submit" disabled={sending || !prompt.trim()}>
          {sending ? 'Working…' : 'Send'}
        </button>
        <p>Enter to send · Shift + Enter for a new line</p>
      </form>
    {:else}
      <div class="empty-state no-session">
        <span aria-hidden="true">◌</span>
        <h2>Create your first conversation</h2>
        <p>Name a conversation in the panel to begin.</p>
      </div>
    {/if}
  </section>
</div>

<style>
  .chat-layout {
    min-height: 100vh;
    padding: 1.1rem;
    display: grid;
    grid-template-columns: 17.5rem minmax(0, 1fr);
    gap: 1.1rem;
  }

  .sessions {
    position: sticky;
    top: 1.1rem;
    height: calc(100vh - 2.2rem);
    padding: 1.15rem 0.8rem;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .sessions-heading {
    padding: 0.2rem 0.35rem 0.85rem;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
  }

  .sessions-heading h2 {
    margin: 0.25rem 0 0;
    font-size: 1.25rem;
    letter-spacing: -0.035em;
  }

  .sessions-heading > span {
    display: grid;
    width: 1.8rem;
    aspect-ratio: 1;
    place-items: center;
    color: var(--muted);
    border-radius: 50%;
    background: var(--surface-soft);
    font-size: 0.7rem;
    font-weight: 700;
  }

  .new-session {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 2.65rem;
    gap: 0.4rem;
  }

  .mobile-session {
    display: none;
  }

  .new-session input,
  .new-session button {
    min-height: 2.55rem;
  }

  .sessions nav {
    min-height: 0;
    margin-top: 0.85rem;
    overflow-y: auto;
  }

  .sessions nav a {
    padding: 0.7rem;
    margin: 0.2rem 0;
    display: grid;
    gap: 0.18rem;
    color: var(--ink);
    border-radius: 0.58rem;
    text-decoration: none;
  }

  .sessions nav a:hover {
    background: var(--surface-soft);
  }

  .sessions nav a.active {
    color: #173f37;
    background: var(--accent-soft);
  }

  .sessions nav span {
    overflow: hidden;
    font-size: 0.82rem;
    font-weight: 680;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sessions nav small {
    color: var(--muted);
    font-size: 0.65rem;
  }

  .conversation {
    width: min(100%, 58rem);
    margin: 0 auto;
    padding: 1.2rem clamp(0.35rem, 2vw, 1.5rem) 8.5rem;
  }

  .conversation > header {
    min-height: 4.8rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  h1 {
    margin: 0.25rem 0 0;
    font-size: clamp(1.55rem, 3vw, 2.25rem);
    line-height: 1.1;
    letter-spacing: -0.05em;
  }

  .scope {
    margin: 0.7rem 0 1.25rem;
  }

  .scope summary {
    min-height: 3.4rem;
    padding: 0.75rem 1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    cursor: pointer;
    list-style: none;
    font-size: 0.82rem;
    font-weight: 720;
  }

  .scope summary::-webkit-details-marker {
    display: none;
  }

  .scope summary small {
    color: var(--muted);
    font-weight: 500;
  }

  .scope-grid {
    padding: 0 1rem 1rem;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.9rem;
    border-top: 1px solid var(--line);
  }

  fieldset {
    min-width: 0;
    max-height: 10rem;
    padding: 0.8rem;
    margin: 0.8rem 0 0;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
  }

  legend {
    padding: 0 0.3rem;
    color: var(--muted);
    font-size: 0.68rem;
    font-weight: 760;
  }

  fieldset label,
  .web-toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.76rem;
  }

  fieldset label + label {
    margin-top: 0.45rem;
  }

  fieldset input,
  .web-toggle input {
    width: 1rem;
    min-height: 1rem;
  }

  fieldset p {
    color: var(--muted);
    font-size: 0.74rem;
  }

  .web-toggle {
    grid-column: 1 / -1;
    padding: 0.75rem;
    border-radius: 0.55rem;
    background: var(--surface-soft);
  }

  .web-toggle span {
    display: grid;
  }

  .web-toggle small {
    color: var(--muted);
  }

  .messages {
    padding: 0.2rem 0.7rem;
  }

  .empty-state {
    min-height: 20rem;
    display: grid;
    place-content: center;
    justify-items: center;
    text-align: center;
  }

  .empty-state > span {
    display: grid;
    width: 3.4rem;
    aspect-ratio: 1;
    place-items: center;
    color: var(--accent-strong);
    border-radius: 1rem;
    background: var(--accent-soft);
    font-size: 1.25rem;
  }

  .empty-state h2 {
    margin: 1rem 0 0.4rem;
    letter-spacing: -0.035em;
  }

  .empty-state p {
    max-width: 30rem;
    margin: 0;
    color: var(--muted);
    line-height: 1.55;
  }

  .no-session {
    min-height: 70vh;
  }

  .stage {
    margin-left: 3rem;
    color: var(--accent-strong);
    font-size: 0.75rem;
    font-weight: 650;
  }

  .composer {
    position: fixed;
    z-index: 10;
    right: max(1.45rem, calc((100vw - 16.5rem - 58rem) / 2 + 1.45rem));
    bottom: 1.15rem;
    width: min(calc(100vw - 16.5rem - 4rem), 55rem);
    padding: 0.65rem;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.55rem;
    box-shadow: var(--shadow);
  }

  .composer textarea {
    min-height: 3.25rem;
    max-height: 10rem;
    padding: 0.75rem;
    border: 0;
    background: transparent;
    box-shadow: none;
  }

  .send {
    align-self: end;
    min-width: 5.4rem;
  }

  .composer p {
    grid-column: 1 / -1;
    margin: -0.2rem 0 0.1rem 0.75rem;
    color: var(--faint);
    font-size: 0.62rem;
  }

  @media (max-width: 1050px) {
    .chat-layout {
      grid-template-columns: 14.5rem minmax(0, 1fr);
    }

    .composer {
      right: 1.4rem;
      width: calc(100vw - 16.5rem - 14.5rem - 3.6rem);
    }
  }

  @media (max-width: 820px) {
    .chat-layout {
      min-height: calc(100vh - 3.75rem);
      padding: 0;
      display: block;
    }

    .sessions {
      position: static;
      width: auto;
      height: auto;
      padding: 0.8rem 1rem;
      border-width: 0 0 1px;
      border-radius: 0;
    }

    .sessions-heading,
    .sessions nav {
      display: none;
    }

    .mobile-session {
      margin-top: 0.55rem;
      display: block;
    }

    .conversation {
      padding: 1rem 1rem 8.5rem;
    }

    .composer {
      right: 0.75rem;
      bottom: 0.75rem;
      width: calc(100vw - 1.5rem);
    }
  }

  @media (max-width: 560px) {
    .scope-grid {
      grid-template-columns: 1fr;
    }

    .web-toggle {
      grid-column: auto;
    }

    .conversation > header button {
      font-size: 0.7rem;
    }

    .composer p {
      display: none;
    }
  }
</style>
