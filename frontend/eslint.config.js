import eslint from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      '.svelte-kit/',
      'build/',
      'coverage/',
      'playwright-report/',
      'test-results/',
      'eslint.config.js',
      'svelte.config.js',
      'playwright.config.ts',
      'tests/mock-api.mjs'
    ]
  },
  eslint.configs.recommended,
  ...tseslint.configs.strictTypeChecked.map((config) => ({
    ...config,
    files: ['**/*.ts', '**/*.svelte']
  })),
  ...svelte.configs['flat/recommended'],
  {
    files: ['**/*.ts', '**/*.svelte'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        extraFileExtensions: ['.svelte']
      }
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-confusing-void-expression': 'off',
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/restrict-template-expressions': 'off'
    }
  },
  {
    files: ['**/*.svelte'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        projectService: true,
        extraFileExtensions: ['.svelte']
      }
    },
    rules: {
      'prefer-const': 'off',
      '@typescript-eslint/no-unnecessary-condition': 'off',
      'svelte/no-navigation-without-resolve': 'off',
      'svelte/require-each-key': 'off'
    }
  },
  {
    files: ['src/lib/components/Markdown.svelte'],
    rules: {
      'svelte/no-at-html-tags': 'off'
    }
  }
);
