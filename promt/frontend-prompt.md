# Правила фронтенда (`frontend/`)

Каталог: `frontend/`. Стек: React 18, TypeScript, Vite, MUI.

## Команды

- **`npm run lint`** — ESLint по `*.ts`, `*.tsx` (ошибки валят выход; предупреждения по хукам и security не блокируют).
- **`npm run format`** — записать стиль Prettier.
- **`npm run format:check`** — проверка форматирования (удобно в CI).
- **`npm run build`** — production-сборка.

Перед коммитом в идеале: `npm run lint && npm run format:check && npm run build`.

## Prettier

- Конфиг: `frontend/.prettierrc.json`, игнор: `frontend/.prettierignore`.
- Охват: `**/*.{ts,tsx,css,json}` из корня `frontend/` (см. `package.json`).
- Не править стиль «вручную» в обход Prettier: спорные правки — через `npm run format`.

## ESLint

- Конфиг: `frontend/.eslintrc.cjs` (ESLint 8, не flat config).
- База: `eslint:recommended`, `plugin:@typescript-eslint/recommended`, `plugin:react-hooks/recommended`, **`prettier`** (последним в `extends`, чтобы отключить конфликты с форматированием).
- **`no-unused-vars` / `@typescript-eslint/no-unused-vars` отключены** — неиспользуемое ловит TypeScript (`noUnusedLocals`, `noUnusedParameters` в `tsconfig.json`).
- **`@typescript-eslint/no-explicit-any` отключён** — включение по проекту отдельным шагом.
- **`react-hooks/exhaustive-deps`: `warn`** — не блокирует `lint`; исправлять осознанно (часто нужны стабильные колбэки или узкие массивы зависимостей).

### Плагин безопасности (`eslint-plugin-security`)

- Подключён пресет **`plugin:security/recommended-legacy`** (не `recommended`: тот рассчитан на ESLint 9 flat config и ломает ESLint 8).
- Правила в основном как **warning** (настройка самого плагина): в т.ч. `detect-object-injection`, `detect-unsafe-regex`, `detect-non-literal-regexp` и др.
- Не отключать предупреждения без причины; для ложных срабатываний — локально обоснованный `eslint-disable-next-line` с комментарием.

### Деньги и IEEE 754 (`eslint-plugin-big-number-rules` + `bignumber.js`)

- Зависимость **`bignumber.js`** в `dependencies`; линтер — в `devDependencies`.
- В **`settings['big-number-rules'].importDeclaration`** задано **`"bignumber.js"`**: правила big-number действуют **только в файлах, где есть импорт из `bignumber.js`**. Так не засоряется весь UI (`i + 1`, индексы и т.д.).
- Все перечисленные в `.eslintrc.cjs` правила **`big-number-rules/*` включены как `error`** в таких файлах (арифметика, сравнения, `Math.*`, `parseFloat`, округления и т.д. — см. пресет плагина).

**Обязательная практика:**

1. **Не считать деньги через нативные** `+`, `-`, `*`, `/`, сравнения чисел с плавающей точкой и «денежные» `Math.*` / `parseFloat` в коде, который относится к суммам.
2. Общие операции с суммами выносить в **`src/utils/money.ts`** и импортировать оттуда (`money`, `moneyAdd`, `moneyMinus`, `moneyTimes`, `moneyDividedBy`, сравнения `moneyEq` / `moneyLt` / …, `moneyToFixed`).
3. Внутри модулей с `import … from "bignumber.js"` использовать API **BigNumber** (`BigNumber(x).plus(y)` и т.п.), а не «голые» операторы.
4. Для цепочек с **`.toFixed`** плагин корректно распознаёт вызов **`BigNumber(value).toFixed(dp)`**; **`new BigNumber(value).toFixed(dp)`** может давать ложное срабатывание `big-number-rules/number` — избегать такой формы.

Если добавляете новый файл с расчётами сумм и импортом `bignumber.js`, держите в нём по возможности только финансовую логику, без смешивания с несвязанной UI-арифметикой.

## Стиль кода в диффах

- Соответствовать существующим паттернам в `src/` (именование, структура страниц, MUI).
- Не раздувать объём правок: без рефакторинга «заодно» несвязанных файлов.
- После правок в `frontend/`: снова `npm run lint` и при необходимости `npm run format`.
