<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import type { Snippet } from 'svelte';
  import { activeRole, type Profile } from '$lib/types';

  let {
    data,
    children
  }: {
    data: { profile: Profile; tenantId: string };
    children: Snippet;
  } = $props();

  let drawerOpen = $state(false);
  let switching = $state(false);
  let switchError = $state('');
  let tenantId = $state('');
  let password = $state('');

  $effect(() => {
    if (!tenantId) tenantId = data.tenantId;
  });

  const navigation = [
    { href: '/chat', label: 'Chat', icon: 'chat' },
    { href: '/documents', label: 'Documents', icon: 'documents' },
    { href: '/collections', label: 'Collections', icon: 'collections' }
  ] as const;

  async function logout(): Promise<void> {
    await fetch('/ui-api/auth/logout', { method: 'POST' });
    await goto('/login', { invalidateAll: true });
  }

  async function switchTenant(): Promise<void> {
    if (tenantId === data.tenantId || !password) return;
    switching = true;
    switchError = '';
    try {
      const response = await fetch('/ui-api/auth/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId, password })
      });
      if (!response.ok) {
        const payload = (await response.json()) as { error?: { message?: string } };
        switchError = payload.error?.message ?? 'Could not switch organizations.';
        return;
      }
      password = '';
      window.location.assign('/chat');
    } finally {
      switching = false;
    }
  }

  function tenantLabel(id: string): string {
    const membership = data.profile.memberships.find((item) => item.tenant_id === id);
    return membership?.name || membership?.slug || 'Organization';
  }
</script>

<svelte:head>
  <meta name="theme-color" content="#14282f" />
</svelte:head>

<div class="app-shell">
  <header class="mobile-header">
    <button
      class="menu-button ghost"
      type="button"
      aria-label="Open navigation"
      aria-expanded={drawerOpen}
      onclick={() => (drawerOpen = true)}
    >
      <span></span><span></span><span></span>
    </button>
    <a class="mobile-brand" href="/chat"><span>K</span> Knowledge Assistant</a>
  </header>

  {#if drawerOpen}
    <button
      class="scrim"
      type="button"
      aria-label="Close navigation"
      onclick={() => (drawerOpen = false)}
    ></button>
  {/if}

  <aside class:open={drawerOpen} aria-label="Application sidebar">
    <div class="brand-row">
      <a class="brand" href="/chat" onclick={() => (drawerOpen = false)}>
        <span class="brand-mark">K</span>
        <span>Knowledge<br />Assistant</span>
      </a>
      <button
        class="close-drawer ghost"
        type="button"
        aria-label="Close navigation"
        onclick={() => (drawerOpen = false)}
      >
        ×
      </button>
    </div>

    <nav aria-label="Primary navigation">
      <p class="nav-label">Workspace</p>
      {#each navigation as item}
        <a
          href={item.href}
          class:active={page.url.pathname.startsWith(item.href)}
          aria-current={page.url.pathname.startsWith(item.href) ? 'page' : undefined}
          onclick={() => (drawerOpen = false)}
        >
          <span class="nav-icon" aria-hidden="true">
            {#if item.icon === 'chat'}◌{:else if item.icon === 'documents'}▤{:else}▦{/if}
          </span>
          {item.label}
        </a>
      {/each}
    </nav>

    <div class="account">
      {#if data.profile.memberships.filter((item) => item.active).length > 1}
        <details>
          <summary>
            <span class="avatar">{data.profile.email.slice(0, 1).toUpperCase()}</span>
            <span class="identity">
              <strong>{tenantLabel(data.tenantId)}</strong>
              <small>{activeRole(data.profile)}</small>
            </span>
          </summary>
          <div class="switcher">
            <label>
              <span>Organization</span>
              <select bind:value={tenantId}>
                {#each data.profile.memberships.filter((item) => item.active) as membership}
                  <option value={membership.tenant_id}>{tenantLabel(membership.tenant_id)}</option>
                {/each}
              </select>
            </label>
            <label>
              <span>Password</span>
              <input
                type="password"
                bind:value={password}
                autocomplete="current-password"
                placeholder="Confirm password"
              />
            </label>
            {#if switchError}<p class="switch-error" role="alert">{switchError}</p>{/if}
            <button
              type="button"
              class="primary"
              disabled={switching || tenantId === data.tenantId || !password}
              onclick={switchTenant}
            >
              {switching ? 'Switching…' : 'Switch organization'}
            </button>
          </div>
        </details>
      {:else}
        <div class="account-summary">
          <span class="avatar">{data.profile.email.slice(0, 1).toUpperCase()}</span>
          <span class="identity">
            <strong>{tenantLabel(data.tenantId)}</strong>
            <small>{activeRole(data.profile)}</small>
          </span>
        </div>
      {/if}
      <p class="email" title={data.profile.email}>{data.profile.email}</p>
      <button class="logout ghost" type="button" onclick={logout}>Sign out</button>
    </div>
  </aside>

  <main class="content">
    {@render children()}
  </main>
</div>

<style>
  .app-shell {
    min-height: 100vh;
    background: var(--surface);
  }

  aside {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 20;
    width: 16.5rem;
    padding: 1.35rem 1rem 1rem;
    display: flex;
    flex-direction: column;
    color: white;
    background:
      radial-gradient(circle at 15% 98%, rgba(123, 214, 175, 0.12), transparent 15rem), var(--nav);
  }

  .brand-row {
    display: flex;
    justify-content: space-between;
  }

  .brand {
    padding: 0.25rem 0.5rem;
    display: flex;
    align-items: center;
    gap: 0.72rem;
    color: white;
    font-size: 0.93rem;
    font-weight: 760;
    line-height: 1.1;
    text-decoration: none;
  }

  .brand-mark,
  .mobile-brand span {
    display: grid;
    width: 2.25rem;
    aspect-ratio: 1;
    place-items: center;
    border-radius: 0.7rem;
    color: #102a2d;
    background: var(--accent);
    font-weight: 850;
  }

  nav {
    margin-top: 3.25rem;
  }

  .nav-label {
    margin: 0 0.7rem 0.7rem;
    color: #758c92;
    font-size: 0.67rem;
    font-weight: 800;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  nav a {
    min-height: 2.85rem;
    padding: 0.65rem 0.78rem;
    margin: 0.15rem 0;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    color: var(--nav-muted);
    border-radius: 0.65rem;
    font-size: 0.91rem;
    font-weight: 650;
    text-decoration: none;
    transition:
      color 130ms ease,
      background 130ms ease;
  }

  nav a:hover {
    color: white;
    background: rgba(255, 255, 255, 0.06);
  }

  nav a.active {
    color: white;
    background: rgba(123, 214, 175, 0.13);
  }

  .nav-icon {
    width: 1.25rem;
    color: var(--accent);
    font-size: 1.15rem;
    text-align: center;
  }

  .account {
    margin-top: auto;
    padding: 0.75rem 0.55rem 0.25rem;
    border-top: 1px solid rgba(255, 255, 255, 0.1);
  }

  details summary,
  .account-summary {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    list-style: none;
  }

  details summary {
    cursor: pointer;
  }

  details summary::-webkit-details-marker {
    display: none;
  }

  .avatar {
    display: grid;
    flex: 0 0 auto;
    width: 2rem;
    aspect-ratio: 1;
    place-items: center;
    color: #11322f;
    border-radius: 50%;
    background: #bce9d6;
    font-size: 0.78rem;
    font-weight: 800;
  }

  .identity {
    min-width: 0;
    display: grid;
  }

  .identity strong {
    overflow: hidden;
    font-size: 0.82rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .identity small {
    margin-top: 0.14rem;
    color: var(--nav-muted);
    font-size: 0.7rem;
    text-transform: capitalize;
  }

  .email {
    margin: 0.85rem 0 0.35rem;
    overflow: hidden;
    color: #80969c;
    font-size: 0.7rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .logout {
    min-height: 2rem;
    padding: 0;
    color: #b7c5c8;
    font-size: 0.75rem;
  }

  .logout:hover {
    color: white;
    background: transparent;
  }

  .switcher {
    margin: 0.8rem -0.15rem 0;
    padding: 0.75rem;
    display: grid;
    gap: 0.65rem;
    color: var(--ink);
    border-radius: 0.7rem;
    background: white;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
  }

  .switcher label {
    display: grid;
    gap: 0.25rem;
    font-size: 0.7rem;
    font-weight: 700;
  }

  .switcher input,
  .switcher select {
    min-height: 2.2rem;
    font-size: 0.75rem;
  }

  .switcher button {
    font-size: 0.72rem;
  }

  .switch-error {
    margin: 0;
    color: var(--danger);
    font-size: 0.7rem;
  }

  .content {
    min-height: 100vh;
    margin-left: 16.5rem;
  }

  .mobile-header,
  .close-drawer,
  .scrim {
    display: none;
  }

  @media (max-width: 820px) {
    .mobile-header {
      position: sticky;
      inset: 0 0 auto 0;
      z-index: 15;
      min-height: 3.75rem;
      padding: 0 0.9rem;
      display: flex;
      align-items: center;
      gap: 0.8rem;
      color: white;
      background: var(--nav);
    }

    .menu-button {
      width: 2.5rem;
      min-height: 2.5rem;
      padding: 0.65rem;
      display: grid;
      gap: 0.25rem;
    }

    .menu-button span {
      height: 2px;
      border-radius: 1px;
      background: white;
    }

    .mobile-brand {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      color: white;
      font-size: 0.84rem;
      font-weight: 760;
      text-decoration: none;
    }

    .mobile-brand span {
      width: 1.9rem;
      border-radius: 0.55rem;
    }

    aside {
      width: min(19rem, 88vw);
      transform: translateX(-105%);
      transition: transform 180ms ease;
    }

    aside.open {
      transform: translateX(0);
    }

    .close-drawer {
      display: inline-flex;
      color: white;
      font-size: 1.5rem;
    }

    .scrim {
      position: fixed;
      inset: 0;
      z-index: 19;
      width: 100%;
      height: 100%;
      display: block;
      border: 0;
      border-radius: 0;
      background: rgba(11, 20, 24, 0.52);
      backdrop-filter: blur(2px);
    }

    .content {
      min-height: calc(100vh - 3.75rem);
      margin-left: 0;
    }
  }
</style>
