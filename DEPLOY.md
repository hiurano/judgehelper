# Памятка по деплою (Judge Helper)

Инструкция по отправке изменений и обновлению продакшн-сервера `serv`.

---

## 📌 Параметры окружения

* **Сервер IP:** `82.40.57.223`
* **Домен:** `https://82.40.57.223.sslip.io`
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

Скрипт `deploy.sh` перед обновлением:

1. проверяет обязательные секреты, домен, права на `.env` и каталоги данных;
2. создаёт согласованную резервную копию SQLite в `/var/backups/judge-helper/`;
3. проверяет конфигурацию Compose, пересобирает контейнеры и ждёт успешного `/ready`.

Ежедневный backup выполняет `judge-helper-backup.timer`. Проверка расписания:

```bash
systemctl list-timers judge-helper-backup.timer
journalctl -u judge-helper-backup.service --since today
```

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
`/var/backups/judge-helper/jobs-<UTC-время>.db`. Если после обновления требуется откат:

```bash
docker compose down
cp /var/lib/judge-helper/jobs.db /var/lib/judge-helper/jobs.failed.db
cp /var/backups/judge-helper/jobs-<UTC-время>.db /var/lib/judge-helper/jobs.db
docker compose up -d
curl -i https://82.40.57.223.sslip.io/ready
```

Подставьте имя нужной копии из `/var/backups/judge-helper/`. Файл `jobs.failed.db`
сохраняется для разбора и не удаляется автоматически.
