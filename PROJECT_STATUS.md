# Judge Helper — состояние проекта и handoff

Дата фиксации: 2026-09-18.

## Текущее состояние

- Production URL: `https://judgehelper.ru`
- Сервер: `serv` (`82.40.57.223`), SSH: `ssh serv`
- Tailscale: `serv.taile2b2a7.ts.net`
- Пользователь deployment: `deploy`
- Код: `/srv/judgehelper` (полноценный read-only deploy-key Git checkout)
- Конфигурация: `/etc/judgehelper`
- Данные: `/var/lib/judgehelper`
- Логи: `/var/log/judgehelper`
- Резервные копии: `/var/backups/judgehelper`
- Системный пользователь приложения: `judgehelper` без shell/login
- GitHub: `https://github.com/hiurano/judgehelper`, ветка `main`
- Production следует за `origin/main`; точный commit проверяется командой
  `git -C /srv/judgehelper rev-parse HEAD`.
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
- Ежедневные SQLite backup выполняются hardened systemd timer от пользователя
  `judgehelper`, хранятся в `/var/backups/judgehelper` и ротируются через 30 дней.
- Preflight проверяет секреты, домен, права `.env`, UID/GID каталогов и runtime-файлов.
- Добавлены Dependabot и dependency audit в CI.
- Удалён устаревший workflow, пинговавший старый Render deployment.

### Проверки

- 41 тест проходит.
- `pip check`: конфликтов зависимостей нет.
- `pip-audit`: известных уязвимостей нет.
- Python compile, Bash syntax и Compose config проходят проверку.
- Ключи AssemblyAI и OpenRouter проверены бесплатными auth-запросами: HTTP 200.
- Production login, `/health`, `/ready` и TLS проверены снаружи.

## Резервные копии последнего deployment

Актуальные ежедневные и pre-deploy копии хранятся в
`/var/backups/judgehelper/`. Также сохранены миграционные копии:

- `/home/deploy/judgehelper/backend/data/backups/predeploy-20260917T221454Z.db`
- `/home/deploy/judgehelper/backend/data/backups/jobs-20260917T221550.809420Z.db`
- `/home/deploy/judgehelper-release-backups/source-20260917T221454Z.tar.gz`

Системные файлы hostname также сохранены с суффиксом
`before-serv-20260917T222814Z` в `/etc` и `/etc/cloud`.

## Известные хвосты для следующего чата

Это рабочие технические долги, а не текущие production-инциденты:

1. Добавить lock-файл с хешами или другой воспроизводимый dependency workflow.
2. Проверить необходимость `build.network: host` и явных публичных DNS на production;
   по возможности заменить системной настройкой Docker DNS/firewall.
3. Добавить автоматический smoke/E2E тест полного пути на тестовом аудиофайле без
   сохранения конфиденциальных данных.
4. Настроить копирование SQLite backup за пределы самого сервера.
5. Проверить лимиты, расходы и retention данных в кабинетах AssemblyAI/OpenRouter.
6. API-ключи были переданы открытым текстом в чате. После стабилизации рекомендуется
   перевыпустить оба ключа и обновить `.env` локально и на production.
7. Удалить локальные остановленные тестовые контейнеры/volumes и legacy-каталоги
   после контрольного периода стабильной работы.

## Рекомендуемый порядок следующей чистки

1. Добавить воспроизводимый lock-файл с хешами.
2. Нормализовать Compose networking для NixOS и production.
3. Добавить внешний backup и полный smoke test.
4. Провести финальную уборку временных файлов, образов и legacy-каталогов.
5. Перевыпустить опубликованные API-ключи и повторить production smoke check.

## Важные эксплуатационные команды

```bash
ssh serv
cd /srv/judgehelper
docker compose ps
docker compose logs -f judgehelper
./scripts/deploy.sh
```

Публичная проверка:

```bash
curl -i https://judgehelper.ru/health
curl -i https://judgehelper.ru/ready
```
