# Памятка по деплою (Judge Helper)

Инструкция по отправке изменений и обновлению продакшн-сервера `serv`.

---

## 📌 Параметры окружения

* **Сервер IP:** `82.40.57.223`
* **Провайдер:** HOSTKEY (панель `invapi.hostkey.ru`), ОС Ubuntu 24.04 LTS
* **Домен:** `https://judgehelper.ru` (DNS ведётся отдельно, в Timeweb)
* **Репозиторий:** `origin main` (GitHub)
* **SSH alias:** `serv`
* **Пользователь SSH:** `deploy`
* **Каталог проекта:** `/srv/judge-helper`
* **Конфигурация:** `/etc/judge-helper`
* **Данные:** `/var/lib/judge-helper`
* **Логи:** `/var/log/judge-helper`
* **Резервные копии:** `/var/backups/judge-helper`

---

## 💻 Шаг 1. Отправка изменений с локального компьютера

Выполняется в терминале на вашей рабочей машине в папке проекта:

```bash
# 1. Проверить статус файлов
git status

# 2. Добавить все изменения в индекс
git add .

# 3. Создать коммит с описанием фактических изменений
git commit -m "описание изменений"

# 4. Отправить в ветку main на GitHub
git push origin main
```

---

## 🌐 Шаг 2. Обновление на продакшн-сервере

1. **Подключиться к серверу по SSH:**
   ```bash
   ssh serv
   ```

2. **Перейти в папку проекта:**
   ```bash
   cd /srv/judge-helper
   ```

3. **Стянуть последние изменения с GitHub:**
   ```bash
   git pull --ff-only origin main
   ```

4. **Пересобрать и запустить Docker-контейнеры:**
   ```bash
   ./scripts/deploy.sh
   ```

---

## ⚡ Быстрое обновление одной командой на сервере

После подключения по SSH можно запустить всё сразу одной строкой:

```bash
cd /srv/judge-helper && git pull --ff-only origin main && ./scripts/deploy.sh
```

Первичная структура production создаётся администратором один раз:

```bash
install -d -o root -g judge-helper -m 0750 /etc/judge-helper
install -d -o judge-helper -g judge-helper -m 0770 \
  /var/lib/judge-helper /var/log/judge-helper /var/backups/judge-helper
```

Файл `/srv/judge-helper/.env` является ссылкой на
`/etc/judge-helper/judge-helper.env` (`root:judge-helper`, mode `640`).

Менять значения в этом файле руками не нужно — для этого есть скрипт, который
сам снимает резервную копию, сохраняет владельца и права и проверяет результат
через `preflight`:

```bash
cd /srv/judge-helper
./scripts/set-config.sh CADDY_DOMAIN=example.ru BASE_URL=https://example.ru
```

Значения он не печатает — только имена изменённых ключей. После правки нужен
обычный деплой, чтобы контейнеры перечитали конфигурацию.

**Root-доступа к серверу нет.** Пароль учётки `deploy` заблокирован
(`passwd -S deploy` → `L`), поэтому `sudo` из-под неё не работает вообще, а
пароль root не задан — вход в VNC-консоль HOSTKEY как `root` тоже не проходит.
Изменить файлы, принадлежащие root (включая
`/etc/judge-helper/judge-helper.env`), можно двумя путями: сбросить root-пароль
в панели HOSTKEY, либо воспользоваться тем, что `deploy` состоит в группе
`docker`, и запустить контейнер с примонтированным `/etc/judge-helper`. Второй
способ работает без перезагрузки, но стоит помнить, что членство в группе
`docker` равносильно root: права `640` на env-файле защищают его от приложения
и посторонних, но не от самого `deploy`.

Скрипт `deploy.sh` перед обновлением:

1. проверяет обязательные секреты, домен, права на `.env` и каталоги данных;
2. создаёт согласованную резервную копию SQLite в `/var/backups/judge-helper/`;
3. проверяет конфигурацию Compose, пересобирает контейнеры и ждёт успешного `/ready`;
4. если `/ready` так и не ответил — печатает логи и **возвращает предыдущий образ**.

Перед пересборкой скрипт помечает тегом `judge-helper:rollback` тот образ, с
которого сейчас работает контейнер. Если новая сборка не проходит проверку
готовности, этот тег возвращается на место и контейнеры пересоздаются из него.
Худший исход неудачного деплоя — «осталась предыдущая версия», а не «сайта нет».
Код возврата в этом случае всё равно ненулевой: деплой не состоялся.

Откат образа не трогает базу данных — её восстановление остаётся ручным (см.
раздел ниже).

Всё содержимое `/var/lib/judge-helper` обязано принадлежать `APP_UID:APP_GID`
(`999:989`, пользователь `judge-helper`). Это касается не только `jobs.db`, но и
служебных файлов SQLite `jobs.db-wal` и `jobs.db-shm`: их создаёт тот процесс,
который открыл базу, и если они останутся от другого пользователя, приложение
не сможет открыть базу вообще — падение выглядит как `unable to open database
file` и бесконечный рестарт контейнера. Поэтому `deploy.sh` снимает
предварительную копию не от имени того, кто его запустил, а от сервисного
аккаунта — в контейнере с `--user APP_UID:APP_GID`, как это делает и
`judge-helper-backup.service`. `preflight` отдельно проверяет владельца обоих
служебных файлов и останавливает деплой до пересборки.

Если такие файлы всё же остались от чужого пользователя, приложение чинится
удалением сайдкаров (сама база не трогается):

```bash
docker compose down
ls -lan /var/lib/judge-helper/          # убедиться, что jobs.db-wal нулевой
rm -f /var/lib/judge-helper/jobs.db-wal /var/lib/judge-helper/jobs.db-shm
docker compose up -d
```

Ненулевой `jobs.db-wal` удалять нельзя — в нём незакоммиченные транзакции.

Ежедневный backup выполняет `judge-helper-backup.timer`. Проверка расписания:

```bash
systemctl list-timers judge-helper-backup.timer
journalctl -u judge-helper-backup.service --since today
```

---

## 🤖 Автоматический деплой

Ручной путь выше остаётся рабочим, но штатный способ выкатки — GitHub Actions.
Job `deploy` в `.github/workflows/ci.yml` запускается **только** после зелёного
`test`, только для ветки `main` и никогда для pull request. Его можно запустить
и вручную — вкладка Actions, кнопка Run workflow, либо:

```bash
gh workflow run "CI Test Suite" --ref main
```

Два деплоя одновременно не пойдут: job объявляет `concurrency` с очередью, а не
с отменой, чтобы начатая выкатка успела либо завершиться, либо откатиться.

Доступ устроен так:

* приватный ключ лежит в secret `DEPLOY_SSH_KEY` репозитория;
* публичный ключ прописан в `~deploy/.ssh/authorized_keys` на сервере
  **с принудительной командой** — ключ не даёт произвольный shell, он умеет
  только выполнить `git pull --ff-only` и `deploy.sh`;
* хост-ключ сервера закреплён в workflow, неизвестный ключ не принимается.

Ограничение честно назвать частичным: `deploy.sh` лежит в репозитории, поэтому
любой, кто может пушить в `main`, и так управляет тем, что выполнится на
сервере. Принудительная команда защищает от другого — от использования самого
ключа, если он утечёт отдельно от доступа к репозиторию.

Строка в `authorized_keys` выглядит так (одной строкой):

```
command="cd /srv/judge-helper && git pull --ff-only origin main && ./scripts/deploy.sh",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... github-actions-deploy@judge-helper
```

Если деплой упал — смотрите лог job. Благодаря откату в `deploy.sh` красный
деплой означает «осталась предыдущая версия», а не «сайт лежит»; убедиться
всё равно стоит по `/ready`.

---

## 🔍 Шаг 3. Проверка и логи

* **Просмотр живых логов бэкенда:**
  ```bash
  docker compose logs -f judge-helper
  ```
  *(для выхода из логов нажмите `Ctrl + C`)*

* **Проверка статуса контейнеров:**
  ```bash
  docker compose ps
  ```

* **Проверка доступности веб-сервера через curl:**
  ```bash
  curl -i https://judgehelper.ru/health
  curl -i https://judgehelper.ru/ready
  ```

  `/health` подтверждает работу процесса, `/ready` возвращает `200`, только когда
  обязательная конфигурация и хотя бы один пользователь действительно готовы.

* **В браузере:**
  Перейти по адресу `https://judgehelper.ru` и обновить страницу с очисткой кэша (`Ctrl + Shift + R` или `Cmd + Shift + R`).

---

## 🔐 Домен и TLS

Сайт обслуживается на `judgehelper.ru`. DNS ведётся в панели Timeweb, обе записи
указывают на сервер:

| Тип | Имя                  | Значение       |
|-----|----------------------|----------------|
| A   | `judgehelper.ru`     | `82.40.57.223` |
| A   | `www.judgehelper.ru` | `82.40.57.223` |

Конфигурация Caddy лежит в `deploy/caddy/Caddyfile` и монтируется в контейнер
только для чтения. Апекс проксируется на приложение, `www` отдаёт постоянный
редирект на апекс. Имя хоста подставляется из `CADDY_DOMAIN`, поэтому смена
домена сводится к правке `/etc/judge-helper/judge-helper.env`.

Сертификат Let's Encrypt Caddy выпускает сам при первом запуске и продлевает
автоматически; состояние ACME хранится в томе `caddy_data` и переживает
пересборку. Для выпуска нужны открытые порты 80 и 443 и уже распространившиеся
DNS-записи.

При смене домена в `/etc/judge-helper/judge-helper.env` меняются две строки
(`preflight` требует, чтобы они совпадали):

```
CADDY_DOMAIN=judgehelper.ru
BASE_URL=https://judgehelper.ru
```

`BASE_URL` используется не только для ссылок: из него собирается webhook-адрес
для AssemblyAI. Если он не совпадает с реально доступным извне именем, callback
не дойдёт и транскрибация свалится в медленный опрос.

Проверка выпущенного сертификата и редиректа:

```bash
curl -sI https://judgehelper.ru/health | head -1
curl -sI https://www.judgehelper.ru | grep -i "^location"
docker compose logs caddy | grep -i "certificate obtained"
```

---

## Откат базы данных

Перед каждым запуском `deploy.sh` база автоматически копируется в
`/var/backups/judge-helper/jobs-<UTC-время>.db`. Если после обновления требуется откат:

```bash
docker compose down
cp /var/lib/judge-helper/jobs.db /var/lib/judge-helper/jobs.failed.db
cp /var/backups/judge-helper/jobs-<UTC-время>.db /var/lib/judge-helper/jobs.db
docker compose up -d
curl -i https://judgehelper.ru/ready
```

Подставьте имя нужной копии из `/var/backups/judge-helper/`. Файл `jobs.failed.db`
сохраняется для разбора и не удаляется автоматически.
