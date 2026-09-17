# Памятка по деплою (Judge Helper)

Инструкция по отправке изменений и обновлению продакшн-сервера `buyerly-prod`.

---

## 📌 Параметры окружения

* **Сервер IP:** `82.40.57.223`
* **Домен:** `https://82.40.57.223.sslip.io`
* **Репозиторий:** `origin main` (GitHub)
* **SSH alias:** `buyerly`
* **Пользователь SSH:** `claude`
* **Каталог проекта:** `/home/claude/judge-helper`

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
   ssh buyerly
   ```

2. **Перейти в папку проекта:**
   ```bash
   cd /home/claude/judge-helper
   ```

3. **Стянуть последние изменения с GitHub:**
   ```bash
   git pull origin main
   ```

4. **Пересобрать и запустить Docker-контейнеры:**
   ```bash
   ./scripts/deploy.sh
   ```

---

## ⚡ Быстрое обновление одной командой на сервере

После подключения по SSH можно запустить всё сразу одной строкой:

```bash
cd /home/claude/judge-helper && git pull origin main && ./scripts/deploy.sh
```

Перед первым запуском убедитесь, что каталоги данных принадлежат UID/GID из `.env`:

```bash
mkdir -p backend/data backend/logs
id -u
id -g
# Внесите полученные числа в APP_UID и APP_GID файла .env, затем:
chown "$(id -u):$(id -g)" backend/data backend/logs
chmod 600 .env
```

Скрипт `deploy.sh` перед обновлением:

1. проверяет обязательные секреты, домен, права на `.env` и каталоги данных;
2. создаёт согласованную резервную копию SQLite в `backend/data/backups/`;
3. проверяет конфигурацию Compose, пересобирает контейнеры и ждёт успешного `/ready`.

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
  curl -i https://82.40.57.223.sslip.io/health
  curl -i https://82.40.57.223.sslip.io/ready
  ```

  `/health` подтверждает работу процесса, `/ready` возвращает `200`, только когда
  обязательная конфигурация и хотя бы один пользователь действительно готовы.

* **В браузере:**
  Перейти по адресу `https://82.40.57.223.sslip.io` и обновить страницу с очисткой кэша (`Ctrl + Shift + R` или `Cmd + Shift + R`).

---

## Откат базы данных

Перед каждым запуском `deploy.sh` база автоматически копируется в
`backend/data/backups/jobs-<UTC-время>.db`. Если после обновления требуется откат:

```bash
docker compose down
cp backend/data/jobs.db backend/data/jobs.failed.db
cp backend/data/backups/jobs-<UTC-время>.db backend/data/jobs.db
docker compose up -d
curl -i https://82.40.57.223.sslip.io/ready
```

Подставьте имя нужной копии из `backend/data/backups/`. Файл `jobs.failed.db`
сохраняется для разбора и не удаляется автоматически.
