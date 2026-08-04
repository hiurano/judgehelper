# ⚖️ JudgeHelper — ИИ-Помощник Судьи и Секретаря

> **Автоматизированный комплекс распознавания устной речи и генерации официальных судебных протоколов по стандарту Нижневартовского городского суда ХМАО-Югры.**

![Python](https://img.shields.io/badge/Python-3.13-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-green.svg)
![AssemblyAI](https://img.shields.io/badge/ASR-AssemblyAI-purple.svg)
![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek--V4--Flash-orange.svg)
![Render](https://img.shields.io/badge/Deployment-Render-black.svg)

---

## 🌟 Ключевые Возможности

- 🎙️ **Сверхточное распознавание речи (ASR):** Мульти-часовая расшифровка диктофонных аудиозаписей судебных заседаний через AssemblyAI.
- ⚖️ **Юридическая адаптация текста:** Трансформация устной речи в официальный судебно-процессуальный стиль без потери деталей.
- 🧠 **Авто-исправление ASR-ошибок:** Встроенный regex-модуль коррекции юридических терминов и специфических фамилий (например, *"Гафуров"*, *"государственного обвинителя"*).
- 📜 **Запрет сырых плейсхолдеров:** Строгий промпт-инжиниринг гарантирует отсутствие скобок вида `[ДАТА]`, `[СТАТЬЯ]` и вымышленных фактов.
- 📄 **Генерация .docx по ГОСТу суда:** Автоматическое формирование документов Word (`Times New Roman 12pt`, одинарный интервал, красная строка `1.25 см`, поля `3-1.5-2-2 см`, выравнивание по ширине).
- ⚡ **SQLite JobStore:** Потоковая асинхронная обработка очереди заданий и сохранение черновиков.

---

## 🛠️ Технологический Стек

- **Backend:** FastAPI (Python 3.13), Uvicorn, SQLite3, `httpx`, `python-docx`
- **AI Services:** OpenRouter (`deepseek/deepseek-v4-flash`), AssemblyAI Speech-to-Text
- **Frontend:** Vanilla JS / HTML5 / CSS3 (Dark Theme & Glassmorphism)
- **CI/CD & Hosting:** GitLab -> Render PaaS

---

## ⚙️ Переменные Окружения (Environment Variables)

Для работы приложения на сервере или локально создайте файл `.env`:

```env
ASSEMBLYAI_API_KEY=your_assemblyai_key
OPENROUTER_API_KEY=your_openrouter_key
WEBHOOK_SECRET=your_webhook_secret
BASE_URL=https://srv-d9361hmgvqtc73a31ukg.onrender.com
LLM_MODEL=deepseek/deepseek-v4-flash
```

---

## 🚀 Локальный Запуск

1. **Клонирование репозитория:**
   ```bash
   git clone https://gitlab.com/hiurano-group/judge-helper.git
   cd judge-helper
   ```

2. **Создание и активация виртуального окружения:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Установка зависимостей:**
   ```bash
   pip install -r backend/requirements.txt
   ```

4. **Запуск сервера FastAPI:**
   ```bash
   python3 -m backend.main
   # или uvicorn backend.main:app --reload --port 8000
   ```

---

## 🏛️ Оформление Судебных Документов (.docx)

Сгенерированные файлы полностью соответствуют требованиям судебного делопроизводства:
- **Шрифт:** Times New Roman, 12 pt
- **Интервал:** 1.0 (Одинарный), 0 pt до/после
- **Красная строка:** 1.25 см
- **Поля:** Левое 3.0 см, Правое 1.5 см, Верхнее 2.0 см, Нижнее 2.0 см
- **Табуляция:** Выравнивание подписей и дат по правому краю на 16.5 см.

---

## 📝 Авторство & Назначение

Разработано для автоматизации рутины и повышения эффективности работы судей и секретарей судебного заседания.
