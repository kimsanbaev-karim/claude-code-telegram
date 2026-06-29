# План: Dockerize claude-code-telegram → выделенный 24/7 git/dev/cloud-бот на Linux VPS

**Создан**: 2026-06-29
**Ветка**: feature/upstream-prs
**Статус**: implemented (2026-06-29 — Шаги 1-8, 12 выполнены; Шаги 9-11 — runbook-инструкции)

## Цель и рамки

Развернуть `claude-code-telegram` в Docker на always-on Linux-VPS как **выделенный 24/7-агент для git/dev/cloud-задач**: клонирует репозитории, правит код, гоняет Linux-тесты, коммитит/пушит, работает с веб и Google Sheets/evo. Доступы (git-логины, Sheets-credentials) пользователь заводит сам.

**Сознательная рамка (подтверждена пользователем после стратегического ревью):** это **дополнение**, а не замена локального бота. Задачи, требующие Rider/Unity/локальных мостов и сборок Windows/Unity, на Linux-VPS **не выполняются** и остаются на локальной машине. Это не баг — это граница профиля.

Корень исходной боли (ноут спит → процесс убит `0xC000013A` → не возвращается) закрывается двумя независимыми механизмами: always-on хост (нет сна) + автоподъём (`restart:unless-stopped` + `docker.service` enabled + healthcheck против зависания).

## Критерии приёмки (наблюдаемые: дано → действие → результат)

- [ ] **Сборка**: `docker build` из исходников ветки `feature/upstream-prs` → exit 0 И внутри образа доступны `claude --version`, `node -v`, `rg --version`, `python -c "import claude_agent_sdk"`.
- [ ] **Старт/поллинг**: `docker compose up -d` → в логе `Polling updates from Telegram started`, нет `Traceback`/`ConfigurationError`/`Conflict`; за ~15с появляется свежий `getUpdates`.
- [ ] **Headless-auth (happy)**: тестовый прогон через бот → модель отвечает; в логе нет ошибок аутентификации.
- [ ] **Headless-auth (negative)**: при заведомо неверном `CLAUDE_CODE_OAUTH_TOKEN` бот **не входит в краш-луп**, в логе различимая сигнатура ошибки auth (для диагностики протухания токена).
- [ ] **Git-задача**: дать боту склонировать заданный (приватный) репозиторий в `/work`, внести правку, прогнать тест, сделать коммит → коммит появляется, push проходит с настроенным доступом.
- [ ] **Зависание ловится (healthcheck)**: при искусственно «зависшем» поллинге healthcheck помечает контейнер `unhealthy` и autoheal перезапускает его → бот снова поллит без ручного вмешательства.
- [ ] **Падение процесса**: `docker kill` контейнера → `restart:unless-stopped` поднимает; **отправил сообщение в Telegram после рестарта → получил ответ** (а не просто «контейнер up»).
- [ ] **Ребут хоста**: `sudo reboot` VPS → после загрузки (Docker enabled at boot) контейнер сам стартует и поллит, без ручных команд.
- [ ] **Персистентность состояния**: создать в `/data` маркер (запись/сессия) → `docker compose down && up` → маркер на месте и читается ботом (sqlite + `task_threads.json` переживают рестарт).
- [ ] **Single-instance**: запуск второго инстанса на том же токене отклоняется guard'ом/виден как `Conflict`; ноутбучный автозапуск отключён насовсем (проверено: не воскреснет при пробуждении ноута).
- [ ] **Vault (выбранная модель single-writer)**: запись бота в vault на сервере доставляется на ноут (read-only зеркало); одновременная правка с двух сторон не приводит к молчаливой потере (versioning ловит конфликт); индекс Basic Memory консистентен после синка.
- [ ] **Безопасность**: из контейнера нет доступа к `docker.sock`/хосту; процесс —非-root; выход Claude за `APPROVED_DIRECTORY=/work` блокируется; egress ограничен allowlist'ом; секреты не лежат под `/work`.
- [ ] **Документация**: `README.docker.md` покрывает auth, secrets, vault-модель, healthcheck/autoheal, ops-runbook (logs с ротацией, restart, обновление образа, ротация годового токена, бэкап/восстановление), границы профиля.

## Шаги (TDD: RED = проверка-провал → GREEN → приёмка)

### Шаг 1: Dockerfile — иммутабельный образ (Python 3.12 + Node + Claude Code CLI), версии запинены
**Сложность**: complex
**RED**: `docker build` падает — Dockerfile нет.
**GREEN**: `python:3.12-slim-bookworm`; системно `git curl ca-certificates ripgrep` + Node из NodeSource **с запиненной мажорной версией** (bookworm-nodejs слишком старый для CLI); `npm i -g @anthropic-ai/claude-code@<pin>`; `uv pip install --system .` (**не** `-e` — код вшит в слой, иммутабельный образ; обновление = ребилд); непривилегированный пользователь `bot`. Версии CLI/SDK/Node — через ARG, зафиксированы.
**Приёмка**: build ок; `claude --version`/`node -v`/`rg --version`/`import claude_agent_sdk` внутри образа.
**Commit**: `build: pinned Dockerfile (py3.12 + node + claude code cli), immutable install`

### Шаг 2: `.dockerignore`
**Сложность**: trivial
**RED**: контекст тянет `.venv/`, `*.log`, `data/`, `__pycache__`.
**GREEN**: `.dockerignore`.
**Приёмка**: `.venv` не в образе, контекст компактный.
**Commit**: `build: add .dockerignore`

### Шаг 3: Voice-deps — только Groq (дроп faster-whisper)
**Сложность**: standard
**RED**: extra `voice` тянет тяжёлый `faster-whisper`, в проде используется Groq (`VOICE_PROVIDER=openai`).
**GREEN**: ставить только `openai` (+`mistralai` при желании), исключить `faster-whisper`.
**Приёмка**: голосовое транскрибируется через Groq; образ без тяжёлых ML-весов.
**Commit**: `build: slim voice deps (openai/Groq only)`

### Шаг 4: Headless-auth + изоляция секретов (security)
**Сложность**: complex
**RED**: `run_command` падает на auth; и/или секреты доступны агенту в `/work`.
**GREEN**:
- `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, годовой, подписка) в env контейнера; убедиться, что конфликтующий `ANTHROPIC_API_KEY` не задан.
- секреты (`.env`, Google creds, git-credentials) **вне `/work`** (рабочая папка агента) — отдельный путь, creds-том read-only;
- **egress-allowlist** на уровне VPS/Docker: наружу только нужные хосты (api.anthropic.com, github, syncthing-пир, api.groq.com, googleapis) — снижает риск эксфильтрации при prompt-injection из склонированного кода/веба;
- non-root, без монтирования `docker.sock`.
**Приёмка**: happy — модель отвечает; negative — неверный токен даёт различимую ошибку без краш-лупа; из контейнера нет `docker.sock`/выхода за `/work`; egress режется.
**Commit**: `feat: headless auth + secret isolation + egress allowlist`
**Источник**: https://code.claude.com/docs/en/authentication, https://code.claude.com/docs/en/devcontainer

### Шаг 5: Секреты/env + git-auth для приватных репо
**Сложность**: standard
**RED**: нет безопасного задания секретов и доступа к приватным репо.
**GREEN**: `.env.docker.example` (полный список переменных, `APPROVED_DIRECTORY=/work`, project-threads, voice/Groq, токен); реальный `.env` только на сервере, в `.gitignore`; git-доступ через SSH deploy-key/PAT (пользователь заводит сам) — путь/монтирование задокументированы.
**Приёмка**: контейнер стартует с `.env`; клон **приватного** репо проходит.
**Commit**: `build: .env.docker.example + git-auth wiring`

### Шаг 6: docker-compose — тома, restart, ротация логов, single-instance guard
**Сложность**: complex
**RED**: нет оркестрации, персистентности, защиты от двойного инстанса, ротации логов.
**GREEN**: `docker-compose.yml`:
- `restart: unless-stopped`; `env_file: .env`;
- тома: `./work:/work` (клоны репо), `./data:/data` (sqlite `DATABASE_URL`, `task_threads.json`), `./config:/app/config`, секреты — отдельным ro-томом вне `/work`;
- `logging: json-file` с `max-size`/`max-file` (ротация);
- **single-instance guard**: lock-файл при старте бота (или внешняя проверка) — два процесса на одной sqlite/json не допускаются;
- `docker.service` enabled at boot (`systemctl enable docker`) — для подъёма после ребута.
**Приёмка**: `up -d` → поллит; `docker kill` → поднялся, бот отвечает; `down/up` → состояние на месте; второй инстанс отклонён; логи ротируются.
**Commit**: `build: compose (restart, volumes, log rotation, single-instance guard)`

### Шаг 7: Healthcheck + autoheal (против «завис, но жив»)
**Сложность**: complex
**RED**: `restart:unless-stopped` не реагирует на зависший процесс (контейнер «здоров», бот не поллит) → регресс к исходной боли.
**GREEN**: healthcheck, проверяющий **реальную живость поллинга** (свежесть последнего `getUpdates`/маркер в логе/heartbeat-файл, обновляемый ботом); т.к. Docker по `restart` **не** перезапускает по `unhealthy` — добавить **autoheal** (sidecar `willfarrell/autoheal` или healthcheck-скрипт, валящий процесс, чтобы `restart` подхватил).
**Приёмка**: искусственно «подвесить» поллинг → контейнер `unhealthy` → autoheal перезапускает → бот снова поллит.
**Commit**: `feat: healthcheck + autoheal for hung-but-alive detection`
**Источник проверить**: поведение Docker restart vs healthcheck (unhealthy ≠ авто-restart) — подтвердить на доке Docker перед реализацией.

### Шаг 8: Vault — модель single-writer + folder-sync
**Сложность**: complex
**RED**: двусторонний Syncthing + SQLite-индекс Basic Memory → `.sync-conflict`/коррупция индекса.
**GREEN**: зафиксирована модель **Obsidian Sync headless** (двусторонняя, мгновенная, E2E):
- `obsidian-headless` (официальный пакет, github.com/obsidianmd/obsidian-headless, docs: https://obsidian.md/help/sync/headless) запускается как **отдельный docker-sidecar** (profile: vault) с Node.js 22+; команды: `ob login` → `ob sync-setup` → `ob sync`
- синхронизируются только markdown-файлы vault; SQLite-индекс Basic Memory (`*.db`, `*.sqlite`) **исключён** из синхронизации
- каждая сторона (ноут / VPS) переиндексирует vault самостоятельно после получения изменений
- конфликты при одновременной правке — разрешаются встроенным механизмом версий Obsidian Sync (conflict copies), не теряются молча
- настройка `obsidian-headless` на VPS — ручная операция пользователя (см. runbook в README.docker.md)
**Приёмка**: изменение vault на ноуте → появляется на VPS в течение нескольких секунд; изменение на VPS-боте → появляется на ноуте; SQLite-файлы не синкаются; одновременная правка → conflict copy (не потеря).
**Commit**: `build: vault single-writer sync model + docs`
**Решение принято**: Obsidian Sync headless (двусторонний, E2E, без git/Syncthing). SQLite-индекс исключён.

### Шаг 9: Локальный smoke на ТЕСТОВОМ токене + замер ресурсов
**Сложность**: standard
**RED**: образ не проверен e2e; боевой токен → 409 с живым ноутом.
**GREEN**: прогон контейнера локально с отдельным тестовым Telegram-ботом/чатом: polling, auth (happy+negative), клон приватного репо+коммит, healthcheck/autoheal, vault-чтение, голос. **Замерить RAM/CPU** при параллельных прогонах (проверить, хватает ли 4 ГБ).
**Приёмка**: все несерверные критерии зелёные; зафиксировано реальное потребление → уточнить спеку VPS.
**Commit**: `docs: local smoke-test + resource measurement`

### Шаг 10: Провижининг VPS + Docker (enabled at boot) + firewall/egress
**Сложность**: standard
**RED**: на сервере нет окружения.
**GREEN**: VPS (спека — по замеру Шага 9, базово ≥2 vCPU/≥4 ГБ/SSD); Docker Engine + compose-plugin, **`systemctl enable docker`**; firewall: inbound только SSH, egress — allowlist (Шаг 4); перенести репозиторий (clone ветки); бэкап-стратегия томов/секретов + процедура восстановления «с нуля».
**Приёмка**: `docker version`/`compose version`; Docker стартует при ребуте; бэкап настроен.
**Commit**: `docs: VPS provisioning + boot-enable + backup/restore`

### Шаг 11: Деплой + cutover (анти-409) + проверка ребута
**Сложность**: complex
**RED**: одновременный поллинг ноут+сервер → 409.
**GREEN**: ноутбучный автозапуск уже удалён насовсем; остановить текущий ноутбучный процесс, пауза (отпустить токен); на сервере `compose up -d`; проверить маркеры/`getUpdates`/нет `Conflict`; затем **`sudo reboot` VPS → бот сам поднялся и поллит**.
**Приёмка**: серверный бот поллит; нет `Conflict`; сообщение обрабатывается сервером; после ребута — авто-подъём.
**Commit**: `docs: cutover + reboot verification`

### Шаг 12: Документация — runbook + границы + ротация токена
**Сложность**: standard
**RED**: нет фиксации эксплуатации и границ → ложные ожидания, тихий отказ токена через год.
**GREEN**: `README.docker.md`: auth, secrets, vault-модель, healthcheck/autoheal; ops — `docker compose logs -f --tail=N`, restart, обновление образа (pull/rebuild → `up -d` → проверка первого ответа), **ротация годового `CLAUDE_CODE_OAUTH_TOKEN`** (зафиксировать дату истечения + календарный ремайндер + сигнатуру ошибки протухания), бэкап/восстановление; **контракт по незавершённым задачам** (прерванный прогон при рестарте НЕ возобновляется — проверь топики, перезапусти вручную); **границы профиля** (нет Rider/Unity/мостов, нет Windows/Unity-сборок).
**Приёмка**: пользователь по runbook может диагностировать/восстановить без подсказок.
**Commit**: `docs: ops runbook + scope limits + token rotation`

## Как закрыты блокеры ревью

- **Healthcheck/зависание** → Шаг 7 (healthcheck + autoheal), критерий «зависание ловится».
- **Безопасность/эксфильтрация** → Шаг 4 (секреты вне `/work`, egress-allowlist, non-root, нет docker.sock), критерий «Безопасность».
- **Vault-коррупция** → Шаг 8 (single-writer + versioning + реиндекс).
- **Ребут хоста** → Шаг 6/10/11 (Docker enabled at boot), критерий «Ребут».
- **Single-instance/409 навсегда** → Шаг 6 (guard) + Шаг 11 (автозапуск ноута удалён).
- **Критерии наблюдаемы** → секция критериев переписана в форме дано/действие/результат.
- **Незавершённые задачи** → Шаг 12 (контракт в runbook).
- **Годовой токен** → Шаг 4 (negative-критерий) + Шаг 12 (ротация/ремайндер).
- **Пины версий / immutable** → Шаг 1 (пины, `uv pip install .` без `-e`).
- **Ротация логов / бэкап / ресурсы** → Шаги 6, 10, 9.

## Pre-PR Quality Gate
- [ ] Образ собирается, контейнер стартует и поллит; smoke на тестовом токене зелёный
- [ ] Healthcheck+autoheal ловят зависание; `restart` + ребут поднимают бот; бот отвечает в Telegram после рестарта
- [ ] Git-задача на приватном репо: клон → правка → тест → коммит/push
- [ ] Vault single-writer без потерь; индекс BM консистентен
- [ ] Безопасность: non-root, нет docker.sock, egress-allowlist, секреты вне /work
- [ ] Cutover без 409; ноутбучный автозапуск отключён
- [ ] `README.docker.md` полон (auth/secrets/vault/healthcheck/ops/ротация/бэкап/границы)
- [ ] Секреты не закоммичены

## Риски и открытые вопросы (для этапа реализации)
- **Vault single-writer**: финальный механизм (Syncthing one-way vs git-синк) — решаем на Шаге 8; нужно согласие, кто «писатель».
- **Перечень приватных репо** и тип git-доступа (deploy-key/PAT) — пользователь заводит сам (Шаг 5).
- **Google-credentials** для Sheets/karim.kanban — пользователь предоставит файл сервис-аккаунта (Шаг 5), доставка ro-томом вне `/work`.
- **Docker restart vs unhealthy**: подтвердить на доке Docker, что нужен autoheal (Шаг 7).
- **Node-версия под CLI**: bookworm-nodejs старый → ставить из NodeSource с пином (Шаг 1).
- **Граница профиля**: задачи Rider/Unity/мосты/Windows-сборки — не на этот сервер (Шаг 12).

## Примечание по процессу
Спек-артефактов (`/specs`) нет — задача инфраструктурная, BDD не профильно; критерии приёмки заданы в наблюдаемой форме. План прошёл 4 ревью-персоны (приёмка/архитектура/UX-оператора/стратегия); стратегический фактор (профиль «дополнение, не замена») разрешён пользователем; технические блокеры внесены в шаги выше.
