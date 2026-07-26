<script lang="ts">
  import { enhance } from '$app/forms';
  import type { ActionData } from './$types';

  let { form }: { form: ActionData } = $props();
  let submitting = $state(false);
</script>

<svelte:head>
  <title>Sign in · Knowledge Assistant</title>
  <meta name="description" content="Sign in to your organization’s private knowledge assistant." />
</svelte:head>

<main class="login-shell">
  <section class="brand-panel" aria-label="Knowledge Assistant">
    <a class="brand" href="/login">
      <span class="brand-mark" aria-hidden="true">K</span>
      <span>Knowledge Assistant</span>
    </a>
    <div class="brand-copy">
      <p class="eyebrow">Private organizational intelligence</p>
      <h1>Answers grounded in the sources your team trusts.</h1>
      <p>
        Search approved documents, organize shared knowledge, and keep every conversation isolated
        to your organization.
      </p>
    </div>
    <p class="security-note">Closed registration · Encrypted session · Tenant isolated</p>
  </section>

  <section class="form-panel">
    <div class="login-card">
      <p class="eyebrow">Welcome back</p>
      <h2>Sign in</h2>
      <p class="muted">Use the credentials provided by your administrator.</p>

      {#if form?.message}
        <div class="alert error" role="alert">{form.message}</div>
      {/if}

      <form
        method="POST"
        use:enhance={() => {
          submitting = true;
          return async ({ update }) => {
            await update();
            submitting = false;
          };
        }}
      >
        <label>
          <span>Email address</span>
          <input
            name="email"
            type="email"
            value={form?.email ?? ''}
            autocomplete="email"
            required
          />
        </label>
        <label>
          <span>Password</span>
          <input name="password" type="password" autocomplete="current-password" required />
        </label>
        <button class="primary large" type="submit" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p class="help">Need access? Contact your organization’s administrator.</p>
    </div>
  </section>
</main>

<style>
  .login-shell {
    min-height: 100vh;
    display: grid;
    grid-template-columns: minmax(22rem, 1.05fr) minmax(24rem, 0.95fr);
    background: var(--surface);
  }

  .brand-panel {
    position: relative;
    overflow: hidden;
    color: white;
    padding: clamp(2rem, 5vw, 5rem);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    background:
      radial-gradient(circle at 85% 16%, rgba(105, 210, 170, 0.22), transparent 28rem),
      radial-gradient(circle at 5% 90%, rgba(255, 202, 119, 0.17), transparent 24rem),
      linear-gradient(145deg, #0f2430 0%, #173a42 58%, #1a4c49 100%);
  }

  .brand-panel::after {
    content: '';
    position: absolute;
    width: 28rem;
    aspect-ratio: 1;
    right: -12rem;
    bottom: -15rem;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 50%;
    box-shadow:
      0 0 0 5rem rgba(255, 255, 255, 0.025),
      0 0 0 10rem rgba(255, 255, 255, 0.018);
  }

  .brand,
  .brand-copy,
  .security-note {
    position: relative;
    z-index: 1;
  }

  .brand {
    display: inline-flex;
    align-items: center;
    gap: 0.75rem;
    width: fit-content;
    color: inherit;
    font-weight: 720;
    letter-spacing: -0.02em;
    text-decoration: none;
  }

  .brand-mark {
    display: grid;
    width: 2.4rem;
    aspect-ratio: 1;
    place-items: center;
    border-radius: 0.75rem;
    background: var(--accent);
    color: #102b2d;
    font-weight: 850;
  }

  .brand-copy {
    max-width: 39rem;
    padding-block: 5rem;
  }

  h1 {
    max-width: 12ch;
    margin: 0.55rem 0 1.35rem;
    font-size: clamp(2.7rem, 5vw, 5.4rem);
    line-height: 0.97;
    letter-spacing: -0.06em;
  }

  .brand-copy > p:last-child {
    max-width: 39rem;
    color: rgba(255, 255, 255, 0.75);
    font-size: clamp(1rem, 1.35vw, 1.2rem);
    line-height: 1.65;
  }

  .security-note {
    color: rgba(255, 255, 255, 0.64);
    font-size: 0.82rem;
    letter-spacing: 0.03em;
  }

  .form-panel {
    display: grid;
    place-items: center;
    padding: 2rem;
  }

  .login-card {
    width: min(100%, 28rem);
  }

  h2 {
    margin: 0.35rem 0;
    font-size: 2.3rem;
    letter-spacing: -0.045em;
  }

  .muted {
    margin: 0 0 2rem;
    color: var(--muted);
  }

  form {
    display: grid;
    gap: 1.15rem;
  }

  label {
    display: grid;
    gap: 0.45rem;
    font-size: 0.9rem;
    font-weight: 650;
  }

  .large {
    min-height: 3.15rem;
    margin-top: 0.25rem;
  }

  .help {
    margin-top: 1.5rem;
    color: var(--muted);
    font-size: 0.85rem;
    text-align: center;
  }

  @media (max-width: 760px) {
    .login-shell {
      grid-template-columns: 1fr;
    }

    .brand-panel {
      min-height: 15rem;
      padding: 1.5rem;
    }

    .brand-copy {
      padding: 2.5rem 0 1rem;
    }

    h1 {
      font-size: clamp(2.1rem, 10vw, 3.4rem);
    }

    .brand-copy > p:last-child,
    .security-note {
      display: none;
    }

    .form-panel {
      padding: 3rem 1.25rem;
      place-items: start center;
    }
  }
</style>
