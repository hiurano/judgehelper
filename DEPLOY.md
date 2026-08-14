# Памятка по деплою (Judge Helper)

Инструкция по отправке изменений и обновлению продакшн-сервера Google Cloud.

---

## 📌 Параметры окружения

* **Сервер IP:** `34.42.63.241`
* **Домен:** `https://34.42.63.241.sslip.io`
* **Репозиторий:** `origin main` (GitHub)
* **Пользователь SSH:** `hiurano`

---

## 💻 Шаг 1. Отправка изменений с локального компьютера

Выполняется в терминале на вашей рабочей машине в папке проекта:

```bash
# 1. Проверить статус файлов
git status

# 2. Добавить все изменения в индекс
git add .

# 3. Создать коммит
git commit -m "feat: Apple Pro Dark UI overhaul, SF Pro font, tab notification fix"

# 4. Отправить в ветку main на GitHub
git push origin main
```

---

## 🌐 Шаг 2. Обновление на продакшн-сервере

1. **Подключиться к серверу по SSH:**
   ```bash
   ssh hiurano@34.42.63.241
   ```

2. **Перейти в папку проекта:**
   ```bash
   cd judge-helper
   ```

3. **Стянуть последние изменения с GitHub:**
   ```bash
   git pull origin main
   ```

4. **Пересобрать и запустить Docker-контейнеры:**
   ```bash
   docker compose up -d --build
   ```

---

## ⚡ Быстрое обновление одной командой на сервере

После подключения по SSH можно запустить всё сразу одной строкой:

```bash
cd judge-helper && git pull origin main && docker compose up -d --build
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
  curl -i https://34.42.63.241.sslip.io/health
  ```

* **В браузере:**
  Перейти по адресу `https://34.42.63.241.sslip.io` и обновить страницу с очисткой кэша (`Ctrl + Shift + R` или `Cmd + Shift + R`).
