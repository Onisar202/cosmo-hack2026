# Правила расследования инцидентов (Investigator / SRE-debug)

Этот документ — операционная карта для агента, который **расследует проблемы в работающем сервисе**: «заявка не создалась», «выплата зависла», «мерчант не получил вебхук», «панель отдаёт 500», «трейдеру не пришёл диспут», «упала конверсия».

Читается вместе с `.ai/main-prompt.md` (раздел 7 «Фактическое состояние прода» — источник истины о том, что деплоится).

> **Все данные ниже проверены на живых хостах** (SSH, `docker ps`, `/etc/traefik/dynamic/*`, Loki API) 2026-07-21. Если реальность расходится с документом — верь `docker ps` и правь этот файл.

---

## 0. Принципы расследования

1. **Read-only по умолчанию.** Расследование не чинит. Никаких `docker restart`, `UPDATE`, `DELETE`, перевыкатов, правок `.env` на хосте — без явного разрешения человека. Даже «безобидный» рестарт уничтожает состояние, которое ты ещё не собрал.
2. **Сначала факты, потом гипотезы.** Каждое утверждение в отчёте должно опираться на конкретный вывод команды (лог-строка, строка БД, HTTP-код). Формулировки «скорее всего», не подкреплённые выводом, помечай как гипотезу явно.
3. **Фиксируй время и часовой пояс.** Все логи и БД — в **UTC**. Пользователь обычно называет время в МСК/IST. Всегда переводи и пиши в отчёте UTC-окно.
4. **Идентификаторы — якорь расследования.** Тянись за `trace_id`, `TX-…` (id заявки), `merchant_id`, `payout_id`, `requisite_id`, `user_id`. Один `trace_id` связывает запись во всех сервисах.
5. **Определи бренд первым делом.** Прод — это **два независимых стенда** (`main` и `aaja`) с раздельными БД. Расследование не на том бренде = потерянный час. Домен из жалобы → бренд (см. §1).
6. **Не чини на проде вручную.** Правка данных в проде — только через владельца сервиса и отдельную задачу; ansible-плейбук перегенерит `.env` и снесёт ручные изменения на хосте.
7. **Секреты не покидают хост.** Не выводи в отчёт значения из `.env`/vault (токены, DSN с паролем, ключи). Только имена ключей и хост/имя БД.

---

## 1. Карта окружений

### Хосты

| Роль | hostname | Публичный IP | VPC IP | Как попасть |
|------|----------|--------------|--------|-------------|
| Edge (Traefik) бренда `main` | `traefic-main` | `104.248.97.20` | `10.104.16.10` | прямой SSH |
| **App-хост бренда `main`** | `prod-main-v1` | нет | `10.104.16.11` | через jump `104.248.97.20` |
| Edge (Traefik) бренда `aaja` | `traefik-aaja` | `167.172.7.50` | `10.104.16.14` | прямой SSH |
| **App-хост бренда `aaja`** | `aaja-v1` | нет | `10.104.16.8` | через jump `167.172.7.50` |
| Edge (Traefik) staging | `ubuntu-s-1vcpu-1gb-sgp1` | `157.230.195.245` | `10.104.16.9` | прямой SSH |
| **App-хост staging** (`staging` + `aajastg`) | `staging-v1` | нет | `10.104.16.5` | через jump `157.230.195.245` |
| Мониторинг (Grafana/Loki/Prometheus) | `moniroting` | нет | `10.104.16.3` | через любой jump |
| CI-раннер GitLab | `gitlab-runner-v1` | нет | `10.104.16.6` | через любой jump |

VPC: `10.104.16.0/20` (DigitalOcean). **Прямого доступа с ноутбука в VPC нет** — только ProxyJump через edge-хост с публичным IP.

### Бренды и surfaces

| Бренд | Env | Домены | Compose-проект | Каталог на хосте | БД | port_base | Дашборд-образ |
|-------|-----|--------|----------------|------------------|-----|-----------|---------------|
| `main` | prod | `inrpay.shop` (API), `panel.inrpay.shop`, `checkout.inrpay.shop` | `inrpay-main` | `/opt/inrpay/main` | `db_main` | 10000 | `frontend` |
| `aaja` | prod | `api.aajapay.com`, `partners.aajapay.com`, `pay.aajapay.com` | `aaja-aaja` | `/opt/aaja/aaja` | `db_aaja` | 10000 | `frontend_new` |
| `staging` | staging | `staging.inrpay.shop`, `panel.staging.inrpay.shop`, `checkout.staging.inrpay.shop` | `inrpay-staging` | `/opt/inrpay/staging` | `db_test` | 19900 | `frontend` |
| `aajastg` | staging | публичных доменов нет (S2S-приёмник по VPC) | `inrpay-aajastg` | `/opt/inrpay/aajastg` | `db_aajastg` | 19700 | — |

БД — **DigitalOcean Managed Postgres**, единый кластер, приватный эндпоинт `private-prod-test-do-user-…-0.h.db.ondigitalocean.com:25060`, у каждого бренда своя база и свой пользователь. Достучаться можно **только из VPC** (с app-хоста).

### Порты (биндятся на `vpc_ip`, наружу не смотрят)

| Смещение | Сервис | Порт `main`/`aaja` | Порт `staging` |
|----------|--------|--------------------|----------------|
| `+1` | `backend` (`server.server:app`) | 10001 | 19901 |
| `+2` | `webui_api` | 10002 | 19902 |
| `+3` | `frontend` (nginx) | 10003 | 19903 |
| `+4` | `backend_webui` | 10004 | 19904 |
| `+5` | `checkout` (nginx) | 10005 | 19905 |

---

## 2. Доступ: как подключаться

### SSH

App-хосты без публичного IP — всегда `-J` (ProxyJump) через edge своего окружения:

```bash
# prod main
ssh -J root@104.248.97.20 root@10.104.16.11
# prod aaja
ssh -J root@167.172.7.50  root@10.104.16.8
# staging (оба бренда на одном хосте)
ssh -J root@157.230.195.245 root@10.104.16.5
# мониторинг
ssh -J root@157.230.195.245 root@10.104.16.3
```

Пользователь везде `root`, ключ  тут `/home/sandbox/files/deploy`. Если `ssh: connect to host 10.104.16.x port 22: Operation timed out` — ты забыл `-J`.

```
Host prod-main   ; HostName 10.104.16.11 ; User root ; ProxyJump root@104.248.97.20
Host aaja        ; HostName 10.104.16.8  ; User root ; ProxyJump root@167.172.7.50
Host staging     ; HostName 10.104.16.5  ; User root ; ProxyJump root@157.230.195.245
Host monitoring  ; HostName 10.104.16.3  ; User root ; ProxyJump root@157.230.195.245
```
(в реальном конфиге — по одной директиве на строку).

### Grafana / Loki / Prometheus

У хоста мониторинга **нет публичного IP** — только SSH-туннель:

```bash
ssh -N -L 3001:10.104.16.3:3001 -L 3100:10.104.16.3:3100 -L 9090:10.104.16.3:9090 root@157.230.195.245
# → Grafana   http://localhost:3001
# → Loki API  http://localhost:3100
# → Prometheus http://localhost:9090
```

### Что запускать локально, а что на хосте

- `docker`, `psql`, `curl` к внутренним портам — **на app-хосте** (в VPC).
- `ansible-playbook` (деплой/откат) — **с ноутбука** из `deploy/` (см. `deploy/docs/staging-deploy.md`), но это уже не расследование.

---

## 3. Сервисы: кто за что отвечает

Все backend-сервисы — **один образ `backend:<tag>`**, различаются только `command`. Проверять актуальный состав: `docker ps`.

| Контейнер (`<project>_<name>`) | Команда | Ответственность | Код |
|--------------------------------|---------|-----------------|-----|
| `backend` | `uvicorn server.server:app` (:8000) | **Публичный API**: создание payin (`/api/get_requisite/`), статус (`/api/status/`), payouts, deposits, info, вебхуки мерчанта, S2S-приём/отдача, admin-ручки. Внутри же крутится фоновый **conversion_monitor_loop** (авто-отключение/восстановление реквизитов, алерты в TG). | `server/`, роутеры `server/routes/*`, монитор `server/services/conversion_monitor.py` |
| `webui_api` | `uvicorn webui_api.main:app` (:8002) | **API дашборда** (legacy): логин, JWT, кабинеты трейдера/мерчанта/саппорта/админа, отчёты и экспорты. Это то, что зовёт `panel.*` под `/api`. | `webui_api/` |
| `backend_webui` | `uvicorn backend_webui.main:app` (:8001) | **Disputes API v2** (SQLAlchemy 2.0): `/api/v2/disputes/*`, `/api/v2/admin/bot-messages`. Живой прод-сервис на обоих брендах. | `apps/backend-webui/` |
| `worker_supbot` | `python -m worker_supbot.main` | **aiogram-воркер саппорт-бота**: форвард диспутов/сообщений в чаты трейдеров и мерчантов, кнопки Accept/Reject. | `apps/worker-supbot/` |
| `expire_worker` | `python -u standalone_worker.py` | **Истечение заявок**: `pending` и `awaiting_confirmation` → expired, возврат холдов, обновление лимитов, callback мерчанту. | `checkers/pandings/standalone_worker.py` |
| `s2s_worker` | `python -u checkers/s2s_worker/standalone_worker.py` | **S2S**: разбор DLQ вебхуков (`webhook_dlq`) и reconciliation балансов/пар транзакций между брендами. Цикл ~10 мин. | `checkers/s2s_worker/` |
| `payin_stat` | `python -u docker_log_monitor.py` | **Алертер**: через docker.sock читает stderr соседних контейнеров, агрегирует ERROR/traceback и шлёт в админ-чат Telegram. Ничего не чинит. | `checkers/payin_stat/docker_log_monitor.py` |
| `frontend` | nginx | Дашборд (React). **Он же реверс-прокси**: `/api/v2/(disputes\|admin)` → `backend_webui`, остальной `/api` → `webui_api`. | `frontend/` (main, staging) / `frontend_new/` (aaja) |
| `checkout` | nginx | Платёжная форма для клиента. | `checkout/` |
| `emulator` (**только staging**) | `python -u scripts/emulator.py` | Генератор нагрузки: payin/payout, доведение до терминальных статусов. На проде **отсутствует**. | `scripts/emulator.py` |

**Чего в проде НЕТ, хотя есть в репозитории и шаблонах compose:** `main_bot` (`bot/main.py`), `support_bot` (`bot/utils/support_bot.py`), `emulator`. Не ищи их логи на проде — контейнеров нет.

**Не приложение, не трогать:** `inrpay_alloy`, `inrpay_cadvisor`, `inrpay_node_exporter`, `inrpay_docker_exporter` (стек мониторинга на каждом хосте).

---

## 4. Логи

### 4.1. Формат

Backend-сервисы пишут **structured JSON** в stdout, по строке на событие:

```json
{"timestamp":"2026-07-21T12:22:16.072997Z","level":"ERROR",
 "message":"HTTP 503 error: Service temporarily unavailable: balance hold failed",
 "service":"server","env":"production",
 "trace_id":"7cc597e53834db4167331a4715292294","span_id":"ab1aa434b84c43da",
 "user_id":"","ip":"35.156.142.188","file":"server.py","line":168}
```

`service`: `server` (backend), `webui_api`, и т.д. `trace_id` — сквозной по запросу; **это главный ключ поиска**. Строки `[TIMER] <METHOD> <path> - N.NNNs` дают тайминги эндпоинтов.

### 4.2. Централизованно — Loki (предпочтительный путь)

Grafana Alloy на каждом хосте шлёт docker-логи **всех** контейнеров в Loki `10.104.16.3:3100`. Лейблы: `container_name`, `host`, `service`, `level`, `compose_service`, `container_image`.

В Loki лежат все окружения сразу (`inrpay-main_*`, `aaja-aaja_*`, `inrpay-staging_*`, `inrpay-aajastg_*` и чужие проекты) — **всегда фильтруй по `container_name`/`host`**.

Через туннель (§2) или прямо с хоста мониторинга:

```bash
# все ERROR у прод-backend за час
ssh -J root@157.230.195.245 root@10.104.16.3 \
  'curl -sG http://localhost:3100/loki/api/v1/query_range \
     --data-urlencode "query={container_name=\"inrpay-main_backend\"} |= \"ERROR\"" \
     --data-urlencode "since=1h" --data-urlencode "limit=100"'

# сквозная трассировка одного запроса по всем сервисам бренда
--data-urlencode 'query={host="prod-main-v1"} |= "7cc597e53834db4167331a4715292294"'

# конкретная заявка
--data-urlencode 'query={host="prod-main-v1"} |= "TX-17846363613213"'

# разбор JSON и фильтр по полю
--data-urlencode 'query={container_name="inrpay-main_backend"} | json | level="ERROR" | file="payments.py"'

# список доступных контейнеров
curl -s http://localhost:3100/loki/api/v1/label/container_name/values
```

Окно задаётся `since=1h` либо `start`/`end` в наносекундах Unix.

### 4.3. Локально — docker logs (когда нужен «сырой» хвост)

```bash
ssh -J root@104.248.97.20 root@10.104.16.11 \
  'docker logs --since 30m --timestamps inrpay-main_backend 2>&1 | grep -i error | tail -50'

# по нескольким сервисам бренда сразу
'for c in $(docker ps --format "{{.Names}}" | grep ^inrpay-main_); do
   echo "== $c"; docker logs --since 15m $c 2>&1 | grep -c ERROR; done'
```

Ретеншн docker-логов ограничен ротацией на хосте — за старым периодом иди в Loki.

### 4.4. Метрики

Prometheus `10.104.16.3:9090` + Grafana `:3001`. Экспортеры: `cadvisor` (CPU/RAM/сеть по контейнерам), `node_exporter` (хост), `docker_exporter`. Полезно для «сервис тормозит / OOM / диск кончился».

---

## 5. База данных

Единый Managed Postgres, у каждого бренда своя база. `psql` **уже установлен на app-хостах**; DSN лежит в `.env` бренда.

```bash
ssh -J root@104.248.97.20 root@10.104.16.11
set -a; . /opt/inrpay/main/.env; set +a     # DB_URL (asyncpg-DSN — DATABASE_URL)
psql "$DB_URL" -tAc "select current_database(), now()"
```

Каталоги `.env` по брендам: `/opt/inrpay/main/.env`, `/opt/aaja/aaja/.env`, `/opt/inrpay/staging/.env`, `/opt/inrpay/aajastg/.env`.

**Правила работы с прод-БД:**
- Только `SELECT`. Любой DML/DDL — стоп, эскалация владельцу.
- Всегда `LIMIT` и явное временное окно: таблицы `applications`/`payouts` большие, полный скан положит пул.
- Пул соединений маленький (prod по умолчанию из vault, staging 2/15). Не открывай десятки сессий, не оставляй висящих транзакций.

**Ключевые таблицы** (`public`, прод-схема на 2026-07-21):

- Заявки и деньги: `applications` (payin), `payouts`, `balance_transactions`, `system_earnings`, `markup`, `rate`, `settings`
- Трейдеры и реквизиты: `users`, `traderrequisite`, `trader_chats`, `trader_deposits`, `trader_deposit_requests`, `trader_exclusive_merchants`, `requisite_rejection_log`, `payout_timer_releases`
- Мерчанты/интеграции: `api_keys`, `log_merch`, `client_blacklist`, `client_rate_limit_logs`, `users_ips`
- S2S: `s2s_brands`, `s2s_audit_logs`, `webhook_dlq`
- Диспуты: `dispute`, `dispute_action`, `dispute_chat_message`, `dispute_proof`, `dispute_settings`, `bot_message_template`, `disputes` (legacy)
- Конверсия/уведомления: `conversion_log`, `notify_trader_broadcast_jobs`
- Саппорт/аудит: `support_users`, `support_user_permissions`, `support_audit_log`, `admin_audit_log`, `permission_definitions`, `chats`, `support_bot`, `provider_chat_bindings`
- Миграции: `alembic_version` (актуальные), `aerich` + `applied_sql_migrations` (legacy)

Расхождение версии кода и `alembic_version` — частая причина 500-х после деплоя: сверяй `docker inspect … .Config.Image` (тег) с `select version_num from alembic_version`.

---

## 6. Edge / Traefik / сеть

Traefik на edge-хостах — **systemd-сервис (бинарь), не контейнер**:

```bash
ssh root@104.248.97.20 'systemctl status traefik --no-pager; journalctl -u traefik --since "30 min ago" | tail -50'
ls /etc/traefik/dynamic/         # _base.yml + <brand>.yml (Ansible managed)
curl -s http://127.0.0.1:8081/api/http/routers | jq   # dashboard/API, только localhost
```

Маршрутизация бренда `main` (в `aaja` — зеркально):

| Домен / путь | → сервис | → адрес |
|--------------|----------|---------|
| `panel.inrpay.shop` | `frontend` (nginx) | `10.104.16.11:10003` |
| `inrpay.shop` | `backend` | `10.104.16.11:10001` |
| `inrpay.shop/api/v2/disputes*`, `/api/v2/admin*` (priority 100) | `backend_webui` | `10.104.16.11:10004` |
| `checkout.inrpay.shop` | `checkout` | `10.104.16.11:10005` |

**`webui_api` (:10002) наружу через Traefik не опубликован** — до него доходят только через nginx внутри `frontend`. Поэтому «панель не работает» может означать проблему в `frontend`-nginx, а не в `webui_api`.

Health-проверки:

```bash
curl -s http://10.104.16.11:10001/api/health     # {"status":"healthy","service":"backend",...}
curl -s http://10.104.16.11:10002/health         # webui_api
curl -s http://10.104.16.11:10004/healthz        # backend_webui
curl -s http://10.104.16.11:10003/health         # frontend nginx
curl -s https://inrpay.shop/api/health           # снаружи, через Traefik
```

Egress: исходящий трафик бренда SNAT-ится через Reserved IP его edge-хоста (`main` → `104.248.97.20`, `aaja` → `167.172.7.50`, staging → `157.230.195.245`) — это IP, который видят провайдеры и мерчанты в whitelist. Проверка: `docker exec <brand>_backend curl -s https://api.ipify.org`.

Для same-host cross-brand (S2S staging→aajastg) критичны маршруты `ip route show table brand_*` — их накатывает роль egress-routing по systemd-таймеру; пропали → ответы молча теряются.

---

## 7. Типовые сценарии

Общая схема: **бренд → окно времени UTC → идентификатор → лог сервиса, который владеет шагом → БД для подтверждения**.

### 7.1. «Клиент не увидел платёжную форму / заявка не создалась»
1. Бренд по домену. Найди запрос: `{container_name="<brand>_backend"} |= "get_requisite"` в окне.
2. Ответ 200 с реквизитом? Если 5xx/пустой пул — смотри `ERROR` рядом (`balance hold failed`, таймауты `get_requisite`).
3. БД: `select id, status, created_at, merchant_id, requisite_id from applications where … order by created_at desc limit 20`.
4. Пул реквизитов: `select count(*) from traderrequisite where payin_enabled = true` — типовая причина нулевой конверсии.
5. Трейдер онлайн? Реквизит не отключён conversion_monitor'ом? → `conversion_log`, `requisite_rejection_log`.

### 7.2. «Заявка висит / истекла не вовремя»
Логи `expire_worker` (`<brand>_expire_worker`) в окне; проверь возврат холда в `balance_transactions` и `status` в `applications`; callback мерчанту (401/таймаут) — там же в логах воркера.

### 7.3. «Мерчант не получил вебхук»
1. `select * from webhook_dlq where … order by created_at desc limit 20` — попал ли в DLQ.
2. Логи `<brand>_s2s_worker` — разбор DLQ, ретраи, `Reconciliation`.
3. `log_merch` — исходящие вызовы к мерчанту.
4. Первый цикл `s2s_worker` сразу после полного деплоя штатно падает на локальных вебхуках («All connection attempts failed») и чинится следующим циклом — не инцидент.

### 7.4. «Выплата зависла в processing»
`payouts` + `balance_transactions` по `payout_id`; логи `backend` (создание) и `s2s_worker` (reconciliation). Исторический класс инцидентов: глобальный circuit breaker, открытый флудом payin-400, глушит создание payout.

### 7.5. «Панель отдаёт 500 / не логинит»
`{container_name="<brand>_webui_api"} |= "ERROR"`; если 502/504 — смотри nginx во `frontend` и живость `webui_api` (`docker ps`, health :10002). Для `/api/v2/disputes|admin` — это `backend_webui`, не `webui_api`. 401 на `/api/v2/*` при валидном логине = рассинхрон JWT-секретов между `webui_api` и `backend_webui`.

### 7.6. «Трейдер не получил диспут / кнопки не работают»
Логи `<brand>_worker_supbot` (aiogram): `chat not found` = у трейдера не проставлен/протух `users.tg_support_chat` или бот не добавлен в чат. Данные — `dispute`, `dispute_action`, `trader_chats`.

### 7.7. «Резко упала конверсия»
`conversion_log` + логи `backend` по `conversion_monitor` (он сам отключает реквизиты и шлёт алерты). Проверь `traderrequisite.payin_enabled`, дубль-превенцию, онлайн трейдеров, `settings`.

### 7.8. «Сломалось после деплоя»
```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'   # тег образа и uptime
cat /opt/inrpay/<brand>/.last_deploy_tag                    # и .last_deploy_tag.prev
```
Сверь тег с `alembic_version`, посмотри `git diff <prev-tag> <tag>`, ошибки старта — в первых строках логов контейнера после рестарта.

### 7.9. Быстрый общий срез здоровья бренда
```bash
ssh -J root@104.248.97.20 root@10.104.16.11 '
  docker ps --format "{{.Names}}\t{{.Status}}";
  for p in 10001/api/health 10002/health 10004/healthz 10003/health; do
    printf "%s -> " "$p"; curl -s -m 5 -o /dev/null -w "%{http_code}\n" "http://10.104.16.11:${p}";
  done;
  for c in $(docker ps --format "{{.Names}}" | grep ^inrpay-main_); do
    printf "%-30s ERR=%s\n" "$c" "$(docker logs --since 15m $c 2>&1 | grep -c ERROR)";
  done'
```

---

## 8. Запрещено при расследовании

- `docker restart` / `docker compose up|down` / `docker rm` на проде.
- Любой DML/DDL в прод-БД, включая «поправить один статус».
- Правка `.env`, `docker-compose.yml`, `/etc/traefik/*` на хосте руками (Ansible managed — снесётся; `restart` контейнера **не перечитывает** `.env`, нужен `up -d`).
- Деплой/откат «чтобы проверить гипотезу».
- Добавление прод-хостов в `deploy/inventory/staging.yml`.
- Вывод секретов (`vault_*`, токены ботов, DSN с паролем) в отчёт, тикет, чат.

Всё это — только по явному решению владельца сервиса, отдельным шагом «починка», а не «расследование».

---

## 9. Формат отчёта

Отчёт всегда пишется **в комментарий задачи Jira** (правила публикации — §10). Ответ в чате — короткая выжимка (2–5 строк) + ссылка на комментарий; полный разбор живёт в задаче, а не в переписке.

```markdown
## Симптом
Что наблюдал пользователь, когда (UTC + локальное), на каком бренде/домене.

## Затронутая область
Бренд, хост, сервисы, идентификаторы (trace_id, TX-…, merchant_id).

## Хронология (UTC)
12:19:14 — <событие> (источник: <команда/лог/запрос>)
12:22:16 — ERROR "balance hold failed" в inrpay-main_backend (server.py:168), trace_id 7cc597…

## Корневая причина
Однозначно, с доказательством. Если не доказана — «Наиболее вероятная причина» + что нужно, чтобы подтвердить.

## Масштаб
Сколько заявок/пользователей/денег затронуто (запрос к БД с числом).

## Что НЕ является причиной
Проверенные и отвергнутые гипотезы — экономит время следующему.

## Рекомендации
- Немедленно (митигация): …
- Постоянное исправление: … (код/файл, если нашёл)
- Мониторинг/алерт, которого не хватило: …

## Воспроизведение
Точные команды, которыми пользовался (копипаст).
```

---

## 10. Публикация результата в Jira

**Результат расследования обязан оказаться в комментарии к задаче Jira.** Расследование, которое осталось только в чате, считается несделанным: через неделю никто не вспомнит, что уже проверяли и что отвергли.

Работа с Jira — через MCP (`jira_get_issue`, `jira_search`, `jira_add_comment`). Общие правила работы с трекером — в `.ai/jira-prompt.md`, здесь только специфика расследований.

### 10.1. Куда писать

| Проект | Назначение | Когда сюда |
|--------|------------|------------|
| `RBNPR` (RubinPr) | баги и жалобы от бизнеса/поддержки (статус-колонка «INRpay баги») | жалоба пришла от продукта/саппорта/мерчанта |
| `RB` (RubinDevelopment) | инженерные задачи, фичи, техдолг | инцидент вскрылся внутри разработки или это дефект уже ведущейся задачи |

Отдельного типа `Bug` в трекере нет — всё заводится типом «Задача»/«Подзадача».

Порядок выбора цели:

1. Пользователь назвал номер (`RB-104`, `RBNPR-78`) — пишем туда.
2. Не назвал — **найди существующую** задачу перед тем, как предлагать новую:
   `jira_search` с JQL вида `project in (RB, RBNPR) AND (summary ~ "payout" OR description ~ "TX-1784…") AND updated >= -60d ORDER BY updated DESC`.
3. Ничего не нашлось — **предложи** создать задачу (проект, заголовок, краткое описание) и **дождись явного «да»**. Сам не создавай.

Если расследование началось в задаче A, но вскрыло отдельный самостоятельный дефект — комментарий с разбором пишем в A, а на новую задачу B (после апрува) ставим ссылку из комментария. Не размазывай один разбор по двум тикетам.

### 10.2. Как писать комментарий

- **Перед публикацией прочитай задачу целиком** (`jira_get_issue` с комментариями). Нужно, чтобы: не продублировать уже написанное коллегой, не противоречить более раннему выводу молча (если опровергаешь — сошлись: «в отличие от комментария от 14.07…»), подхватить контекст (какой бренд, какой мерчант).
- **Один комментарий = один законченный вывод.** Полный шаблон из §9. Не нарезай отчёт на пять реплик.
- **Язык — русский**, термины (payin, payout, requisite, DLQ, trace_id) — как есть.
- **Форматирование:** markdown поддерживается — заголовки, списки, `inline code`, ```-блоки. Таблицы в комментариях выглядят плохо, вместо них — списки.
- **Логи — цитатой, дозированно.** Не больше ~20 строк в ```-блоке, только релевантные. Остальное — LogQL-запрос, которым это воспроизводится (`{container_name="inrpay-main_backend"} |= "TX-…"`, окно UTC), а не портянка на 500 строк.
- **Обязательный технический контекст** в каждом комментарии, иначе через месяц он бесполезен:
    - бренд и окружение (`main` / prod);
    - окно инцидента в **UTC** (и локальное в скобках);
    - версия образа на момент инцидента (`docker ps` → тег, например `backend:v0.2.16`);
    - идентификаторы (`trace_id`, `TX-…`, `merchant_id`, `payout_id`).
- **Долгое расследование — промежуточный комментарий** («что уже проверено, что отвергнуто, куда копаю») и финальный с выводом. Молчать несколько часов в тикете, который ждут, нельзя.
- **Статус подтверждённости явно:** «Корневая причина подтверждена: <доказательство>» либо «Гипотеза, для подтверждения нужен <доступ/лог/повтор>». Гипотеза, оформленная как факт, — худшее, что можно оставить в тикете.

### 10.3. Чего в комментарии быть не должно

- Секретов: токенов ботов, `SECRET_KEY`/JWT, DSN с паролем, содержимого `.env`/vault. Только имена ключей («расходится `WEBUI_SECRET_KEY` между сервисами»).
- PII клиентов: полные номера карт/UPI/телефонов — маскируй (`****1234`), ФИО не тащим.
- Скриншотов терминала вместо текста — текст ищется поиском, картинка нет.

### 10.4. Границы прав в Jira

Расследователь **добавляет комментарий** — и всё. Без явного разрешения человека **не**:

- меняет описание задачи (`jira_update_issue`) — там продуктовые требования, они не твои;
- двигает статус (`jira_transition_issue`);
- создаёт задачи и подзадачи (`jira_create_issue`);
- переназначает исполнителя, меняет приоритет, удаляет что-либо.

Предложить всё это в тексте комментария («предлагаю завести отдельную задачу на X, перевести в Тестирование») — правильно и полезно. Сделать самому — нет.
