import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import prettier from 'eslint-config-prettier'

export default tseslint.config(
  { ignores: ['dist', 'coverage'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Unused locals/parameters are caught by TypeScript (noUnusedLocals/noUnusedParameters
      // in tsconfig.app.json, see .ai/frontend-prompt.md) — disable the ESLint duplicate.
      '@typescript-eslint/no-unused-vars': 'off',
      // Advisory only: fixing a missing dep is a deliberate call, not a build blocker.
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
  prettier,
)
