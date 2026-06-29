# README.docker.md — Ops Runbook: 24/7 VPS-агент

## 1. Обзор и границы профиля

### Что умеет
- git-операции: клонировать репо, ветки, checkout, merge, rebase
- Разработка: читать/писать/рефакторить код, запускать тесты
- Коммиты и push в удалённые репозитории
- Работа с вебом: HTTP-запросы, парсинг, браузерные инструменты
- Google Sheets / Google Workspace через karim.kanban и gws-скиллы
- Cloud-задачи: CI/CD, конфигурации, скрипты деплоя

### Чего НЕ умеет (границы)
- Rider / Unity / Visual Studio — Windows-IDE и Windows-сборки остаются на ноуте
- Локальные мосты (WhatsApp bridge, Castle, PW_Game.exe) — только ноут
- Задачи, требующие локального GUI или доступа к Windows-процессам
- Работа с локальными файлами ноута напрямую (только через git-репо или cloud)

---

## 2. Предварительные требования

### VPS
- Минимум: 2 vCPU, 4 ГБ RAM, 20 ГБ SSD
- ОС: Debian 12 или Ubuntu 22.04 LTS (рекомендовано)
- Статический IP или DNS-имя

### Docker
- Docker Engine + compose-plugin (не docker-compose v1)
- `systemctl enable docker` — выполняется один раз при провижининге
- Проверка: `docker compose version` должен вернуть v2.x

### Git-аутентификация
- Deploy-key или PAT (Personal Access Token) для приватных репо
- Настраивается пользователем вручную: `~/.ssh/config` или `~/.gitconfig`

### Google service-account
- JSON-файл с credentials для Sheets/karim.kanban
- Пользователь предоставляет и кладёт в `./secrets/google-sa.json`

---

## 3. Аутентификация Claude (headless OAuth)

### Получение токена
На машине с авторизованным Claude Code выполнить:
```bash
claude setup-token
```
Скопировать выведенный токен.

### Размещение токена
Поместить в `.env` файл на VPS:
```
CLAUDE_CODE_OAUTH_TOKEN=<ваш_токен_здесь>
```

### Важные правила
- **НЕ устанавливать** `ANTHROPIC_API_KEY` одновременно с `CLAUDE_CODE_OAUTH_TOKEN` — они конфликтуют
- Токен **годовой**: зафиксировать дату получения, поставить напоминание за 30 дней до истечения

### Диагностика протухшего токена
В логах контейнера появятся строки:
```
AuthenticationError
Invalid token
```
Это однозначная сигнатура — токен нужно ротировать (см. раздел 9, «Ротация CLAUDE_CODE_OAUTH_TOKEN»).

---

## 4. Секреты и изоляция

### Правила хранения
- `.env` — только на сервере, никогда не коммитить в git
- `.env` добавлен в `.gitignore` проекта
- `./secrets/` — монтируется как **read-only том** внутри контейнера по пути `/secrets`
- Секреты не попадают в `/work` (рабочую директорию агента)

### docker.sock
- `docker.sock` **не монтируется** в контейнер `bot`
- Монтируется только в `autoheal` sidecar для управления контейнерами

### Структура томов
```
./data/       → /data        (rw)  — SQLite БД, task_threads.json, heartbeat
./config/     → /config      (ro)  — конфигурация агента
./secrets/    → /secrets     (ro)  — Google SA, SSH-ключи
./work/       → /work        (rw)  — рабочие клоны репо
```

---

## 5. Vault — Obsidian Sync headless

Vault Basic Memory синхронизируется через `obsidian-headless` (npm-пакет). Это **ручная операция** пользователя при первоначальной настройке.

### Установка и настройка
```bash
# Установить obsidian-headless на VPS
npm install -g obsidian-headless

# Настроить с учётными данными Obsidian Sync (пользователь вводит сам)
obsidian-headless login

# Указать путь к vault (должен совпадать с vault на ноуте)
obsidian-headless sync --vault /path/to/vault --daemon
```

### Исключения из синхронизации (важно!)
- `*.db` и `*.sqlite` — SQLite-индекс Basic Memory **не синхронизируется**
- Каждая сторона (ноут / VPS) переиндексирует vault самостоятельно после получения файлов

### Модель конфликтов
Obsidian Sync создаёт conflict copies при одновременной правке с обеих сторон — данные не теряются молча.

### Критерий приёмки
- Запись заметки на ноуте → через несколько секунд файл появляется на VPS
- Запись бота на VPS → через несколько секунд файл появляется на ноуте
- `*.db` / `*.sqlite` не синхронизируются (ожидаемое поведение)

---

## 6. Сборка образа

```bash
git clone --branch feature/upstream-prs https://github.com/... claude-code-telegram
cd claude-code-telegram
docker build -t claude-telegram-bot:latest .
```

### Зафиксированные версии (ARG в Dockerfile)
| Компонент       | Версия |
|-----------------|--------|
| Node.js         | 22     |
| Claude Code CLI | 1.0.33 |
| Poetry          | 2.1.3  |

Обновление версии = rebuild с изменённым `ARG` в Dockerfile.

---

## 7. Первый запуск

```bash
# Создать рабочие директории
mkdir -p work data config secrets

# Скопировать шаблон .env и заполнить
cp .env.docker.example .env
nano .env  # заполнить TELEGRAM_BOT_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, ALLOWED_USERS и др.

# Запустить
docker compose up -d

# Проверить логи первого старта
docker compose logs -f bot --tail=50
```

### Ожидаемый вывод при успешном старте
В логах должно появиться:
```
Starting bot  mode=polling
```
**Не должно быть:** `Traceback`, `ConfigurationError`, `Conflict`.

---

## 8. Healthcheck и autoheal

### Механизм
- Бот пишет timestamp в `/data/heartbeat` каждые ~30 секунд
- `healthcheck.sh` проверяет свежесть файла (порог: 120 секунд)
- `autoheal` sidecar перезапускает контейнер при статусе `unhealthy`
- `restart: unless-stopped` поднимает контейнер после падения процесса

### Автозапуск при ребуте VPS
Docker включён в автозапуск (`systemctl enable docker`) → контейнер стартует автоматически после перезагрузки сервера.

### Диагностика
```bash
# Статус healthcheck
docker inspect claude-telegram-bot | grep -A5 Health

# Лог autoheal sidecar
docker compose logs autoheal --tail=20
```

---

## 9. Ops-runbook

### Просмотр логов
```bash
# Потоком в реальном времени
docker compose logs -f bot --tail=100

# За последний час
docker compose logs bot --since=1h
```

### Перезапуск
```bash
docker compose restart bot
```

### Обновление образа
```bash
git pull
docker build -t claude-telegram-bot:latest .
docker compose up -d bot

# Проверить первый ответ в Telegram после обновления
docker compose logs -f bot --tail=20
```

### Ротация CLAUDE_CODE_OAUTH_TOKEN (раз в год)
1. На машине с авторизованным Claude Code выполнить: `claude setup-token`
2. Скопировать новый токен
3. Обновить `.env` на VPS: `CLAUDE_CODE_OAUTH_TOKEN=<новый_токен>`
4. Перезапустить: `docker compose restart bot`
5. Проверить логи — нет строк `AuthenticationError`
6. Поставить напоминание на следующий год

### Бэкап
```bash
# Архивировать данные бота
tar -czf backup-$(date +%Y%m%d).tar.gz ./data/ ./config/ ./secrets/
```
Vault (Basic Memory) синхронизируется через Obsidian Sync — он сам является распределённым бэкапом.

### Восстановление с нуля
1. Провизионировать VPS, установить Docker (см. раздел 12)
2. `git clone` репозитория, `docker build`
3. Восстановить `./data/`, `./config/`, `./secrets/` из архива бэкапа
4. Заполнить `.env`
5. `docker compose up -d`
6. Запустить `obsidian-headless sync` для восстановления vault

---

## 10. Предотвращение конфликта 409 (два поллера)

Telegram API запрещает два одновременных `getUpdates`-подключения к одному боту.

### Правило переключения
1. Остановить бот на ноуте
2. Подождать 5 секунд
3. Запустить на сервере

### Признак конфликта в логах
```
Conflict: terminated by other getUpdates request
```
При появлении этой строки — найти и остановить второй экземпляр бота.

### Профилактика
- Ноутбучный автозапуск бота должен быть **отключён** перед запуском серверного
- Проверить: автозапуск убран из планировщика / systemd / launchd на ноуте

---

## 11. Незавершённые задачи при рестарте

Если контейнер перезапустился во время выполнения задачи — задача **не возобновляется автоматически**.

**Действия:**
1. Проверить активные топики в Telegram
2. Убедиться, что задача не завершилась частично (проверить коммиты / файлы)
3. Перезапустить задачу вручную в нужном топике

---

## 12. Серверные шаги (manual ops, не автоматизированы)

Следующие шаги выполняются вручную при первоначальном провижининге сервера:

- **Провижининг VPS**: выбор провайдера (Hetzner, DigitalOcean, Linode и др.), создание инстанса с нужными характеристиками (2 vCPU / 4 ГБ RAM / 20 ГБ SSD / Debian 12)
- **Установка Docker Engine**: по официальной инструкции для дистрибутива, включая compose-plugin
- **Активация автозапуска**: `systemctl enable docker` — один раз, до первого запуска контейнеров
- **Настройка firewall**:
  - Inbound: только SSH (порт 22)
  - Egress allowlist: `api.anthropic.com`, `github.com`, `api.groq.com`, `googleapis.com` и другие необходимые эндпоинты
- **Git deploy-key**: генерация SSH-ключа, добавление публичной части в репозиторий, приватного — в `~/.ssh/`
- **Google service-account credentials**: получить JSON-файл от пользователя, поместить в `./secrets/google-sa.json`
- **Obsidian Sync headless**: установка `obsidian-headless`, login, настройка sync daemon (см. раздел 5)
- **Cutover**: остановить ноутбучный бот → убедиться в отсутствии активных задач → запустить серверный (`docker compose up -d`)
