import type { TokenBundle } from '$lib/server/session';

declare global {
  namespace App {
    interface Locals {
      session: TokenBundle | null;
    }
  }
}

export {};
