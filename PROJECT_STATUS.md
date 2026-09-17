# Judge Helper — состояние проекта и handoff

Дата фиксации: 2026-09-18.

## Текущее состояние

- Production URL: `https://82.40.57.223.sslip.io`
- Сервер: `serv` (`82.40.57.223`), SSH: `ssh serv`
- Tailscale: `serv.taile2b2a7.ts.net`
- Пользователь deployment: `deploy`
- Каталог на сервере: `/home/deploy/judge-helper`
- GitHub: `https://github.com/hiurano/judge-helper`, ветка `main`
- Проверенный релиз: `8cc9702`
- Последующие служебные коммиты: `5043ede`, `d0b1b67`
- Production backend и Caddy запущены; `/health` и `/ready` возвращают HTTP 200.
- TLS-сертификат для production URL валиден.
- В базе на момент обновления сохранены две существующие задачи.

Секреты и пароль администратора намеренно не записаны в этот документ. Они находятся
только в `.env`/production-базе и должны храниться отдельно от Git.

## Выполненные изменения

### Безопасность и авторизация

- Удалено автоматическое создание учётной записи `admin/admin`.
- Добавлена безопасная первичная инициализация администратора и миграция старого
  слабого аккаунта.
- Смена пароля отзывает ранее выданные сессии.
- Добавлена изоляция задач по владельцам.
- Webhook закрыт при отсутствии/несовпадении секрета.
- Добавлено ограничение попыток входа.
- Добавлены проверки сигнатуры загружаемого медиа, размеров файлов, форм и текста.
- Добавлено атомарное ограничение числа активных задач пользователя.
- Сырые транскрипты не сохраняются и не возвращаются клиенту.
- Контейнер приложения запускается без root, с read-only root filesystem,
  `no-new-privileges` и без Linux capabilities.

### Надёжность приложения

- Добавлено управление жизненным циклом фоновых задач и корректное завершение.
- Исправлены восстановление задач, polling, retry-статусы и конкурентная обработка.
- Исправлены разбиение длинных транскриптов и подсчёт использования LLM.
- Настроена явная цепочка резервных LLM-моделей.
- Обновлены заголовок и лимит токенов OpenRouter под текущий API.
- Добавлены `/health` и строгий `/ready`.
- Добавлена ротация логов.

### Production и эксплуатация

- Python-образ обновлён до `3.12-slim`.
- Caddy закреплён на `2.11.4-alpine`.
- Каталог `prompts` подключается read-only.
- Добавлены Compose healthcheck и ожидание healthy перед запуском Caddy.
- Для NixOS/Tailscale сборка использует host network; runtime-сервисы имеют явные DNS.
- Добавлены `scripts/preflight.py`, `scripts/backup_db.py`, `scripts/deploy.sh`.
- `deploy.sh` работает и на NixOS без глобального Python через одноразовый Python-контейнер.
- Перед deployment создаётся согласованная SQLite-копия.
- Preflight проверяет секреты, домен, права `.env`, UID/GID каталогов и runtime-файлов.
- Добавлены Dependabot и dependency audit в CI.
- Удалён устаревший workflow, пинговавший старый Render deployment.

### Проверки

- 40 тестов проходят.
- `pip check`: конфликтов зависимостей нет.
- `pip-audit`: известных уязвимостей нет.
- Python compile, Bash syntax и Compose config проходят проверку.
- Ключи AssemblyAI и OpenRouter проверены бесплатными auth-запросами: HTTP 200.
- Production login, `/health`, `/ready` и TLS проверены снаружи.

## Резервные копии последнего deployment

На сервере сохранены:

- `/home/deploy/judge-helper/backend/data/backups/predeploy-20260917T221454Z.db`
- `/home/deploy/judge-helper/backend/data/backups/jobs-20260917T221550.809420Z.db`
- `/home/deploy/judge-helper-release-backups/source-20260917T221454Z.tar.gz`

Системные файлы hostname также сохранены с суффиксом
`before-serv-20260917T222814Z` в `/etc` и `/etc/cloud`.

## Известные хвосты для следующего чата

Это рабочие технические долги, а не текущие production-инциденты:

1. Локальный каталог `/home/hiurano/Projects/judge-helper` содержит неполную `.git`.
   Канонический исправный checkout текущей сессии находился в `/tmp`; GitHub уже содержит
   все коммиты. Нужно заново клонировать репозиторий в постоянный каталог и аккуратно
   перенести локальный `.env`, private prompt и runtime data.
2. `/home/deploy/judge-helper` на сервере также не является Git checkout. Текущий релиз
   доставлен проверенным архивом. Нужно либо заново клонировать репозиторий с сохранением
   `.env`, `backend/data`, `backend/logs` и private prompt, либо официально закрепить
   archive-based deployment и исправить `DEPLOY.md` под него.
3. `pytest` находится в общем `requirements.txt` и попадает в production-образ. Разделить
   runtime и development/test зависимости.
4. Добавить lock-файл с хешами или другой воспроизводимый dependency workflow.
5. Проверить необходимость `build.network: host` и явных публичных DNS на production;
   по возможности заменить системной настройкой Docker DNS/firewall.
6. Добавить автоматический smoke/E2E тест полного пути на тестовом аудиофайле без
   сохранения конфиденциальных данных.
7. Настроить регулярное внешнее резервное копирование SQLite и ротацию старых backup-файлов.
8. Проверить лимиты, расходы и retention данных в кабинетах AssemblyAI/OpenRouter.
9. API-ключи были переданы открытым текстом в чате. После стабилизации рекомендуется
   перевыпустить оба ключа и обновить `.env` локально и на production.
10. Удалить локальные остановленные тестовые контейнеры/volumes и временные release checkout
    только после восстановления постоянного Git checkout.

## Рекомендуемый порядок следующей чистки

1. Восстановить постоянный Git checkout локально и на сервере.
2. Разделить runtime/dev dependencies и уменьшить Docker image.
3. Нормализовать Compose networking для NixOS и production.
4. Автоматизировать backup retention и smoke tests.
5. Провести финальную уборку документации, временных файлов, образов и volumes.
6. Перевыпустить опубликованные API-ключи и повторить production smoke check.

## Важные эксплуатационные команды

```bash
ssh serv
cd /home/deploy/judge-helper
docker compose ps
docker compose logs -f judge-helper
./scripts/deploy.sh
```

Публичная проверка:

```bash
curl -i https://82.40.57.223.sslip.io/health
curl -i https://82.40.57.223.sslip.io/ready
```
