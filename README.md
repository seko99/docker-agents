# Codex in Docker

## Что делает конфигурация
- Запускает `codex` в контейнере.
- В контейнер монтируется только каталог проекта (`PROJECT_DIR` -> `/workspace`).
- `codex` стартует с флагом `--dangerously-bypass-approvals-and-sandbox`.
- Добавлен сервис `codex-exec` для неинтерактивного запуска `codex exec` с промптом из переменной окружения.
- Корневая ФС контейнера read-only, writable только bind-монт проекта и tmpfs (`/tmp`, `/root`).
- Данные авторизации `codex` сохраняются в `CODEX_HOME_DIR` (`./.codex-home` по умолчанию).
- В образ включен Go-стек: `go`, `golangci-lint v2`, `swag`, `protoc`, `protoc-gen-go`, `protoc-gen-go-grpc`, `git`, `curl`, `jq`, `rg`, `make`, `docker` (CLI).
- Для `testcontainers` используется отдельный внутренний сервис `dockerd`, без проброса `docker.sock` хоста в `codex`.
- Git remote-операции разрешены только по secure-протоколам (`ssh`/`https`); `git://` блокируется.

## Использование
1. Переменные лежат в `.env` (уже заполнен). При необходимости правьте:

```bash
PROJECT_DIR=/absolute/path/to/your/project
CODEX_HOME_DIR=/absolute/path/to/codex-home
HOST_SSH_DIR=/home/your-user/.ssh
HOST_GITCONFIG=/home/your-user/.gitconfig
LOCAL_UID=1000
LOCAL_GID=1000
GOPRIVATE=gitlab.yourdomain.org/*
GONOSUMDB=gitlab.yourdomain.org/*
GONOPROXY=gitlab.yourdomain.org/*
GIT_ALLOW_PROTOCOL=file:https:ssh
CODEX_PROMPT=
CODEX_EXEC_FLAGS=--dangerously-bypass-approvals-and-sandbox
```

Для `PROJECT_DIR`, `CODEX_HOME_DIR`, `HOST_SSH_DIR` и `HOST_GITCONFIG` используйте абсолютные пути.
Относительный путь через переменную окружения, например `PROJECT_DIR=./`, на некоторых Docker/Compose окружениях приводит к ошибкам bind mount. Если нужно передать текущий каталог через shell, используйте `PROJECT_DIR="$PWD"`.

Для нового окружения можно взять шаблон `.env.example`.
Кэши `go`/`golangci-lint` хранятся в `CODEX_HOME_DIR`, поэтому повторные прогоны заметно быстрее.

2. Один раз выполните вход по подписке (интерактивно):

```bash
docker-compose run --rm codex-login
```

`codex-login` использует `network_mode: host`, чтобы OAuth callback на `localhost` был доступен из браузера хоста.

3. Рабочий запуск агента:

```bash
docker-compose run --rm codex
```

4. Неинтерактивный запуск с готовым промптом:

```bash
docker-compose run --rm \
  -e CODEX_PROMPT="Проверь проект и исправь failing тесты" \
  codex-exec
```

Если удобнее держать промпт в `.env`, можно задать `CODEX_PROMPT` там и запускать короче:

```bash
docker-compose run --rm codex-exec
```

По умолчанию `codex-exec` запускает `codex exec --dangerously-bypass-approvals-and-sandbox`, чтобы режим совпадал с интерактивным `codex`. Флаги можно переопределить через `CODEX_EXEC_FLAGS`, например:

```bash
docker-compose run --rm \
  -e CODEX_PROMPT="Сделай обзор изменений в репозитории" \
  -e CODEX_EXEC_FLAGS="--full-auto" \
  codex-exec
```

## Go-команды внутри контейнера

```bash
docker-compose run --rm codex bash -lc "go test ./..."
docker-compose run --rm codex bash -lc "golangci-lint run ./..."
docker-compose run --rm codex bash -lc "swag init -g cmd/main.go -o docs/swagger"
docker-compose run --rm codex bash -lc "protoc --version && which protoc-gen-go && which protoc-gen-go-grpc"
docker-compose run --rm codex bash -lc "go version && golangci-lint --version"
```

## Примечания по безопасности
- `codex` контейнер не получает `docker.sock` хоста.
- Доступ к Docker для тестов идет через изолированный `dockerd` в этой же compose-сети.
- Сервис `dockerd` запущен `privileged` (техническое требование DinD); это безопаснее, чем отдавать агенту доступ к Docker хоста, но не равно полной sandbox-изоляции.
