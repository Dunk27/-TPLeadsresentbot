# 🚀 Деплой бота на Render.com — пошаговая инструкция

## Из чего состоит система (всё бесплатно)

```
GitHub → Render.com (бот работает) ← cron-job.org (не даёт уснуть)
```

---

## Шаг 1 — Обновить файлы на GitHub

Замените в своём репозитории старые файлы на новые:

| Файл | Что делать |
|---|---|
| `bot.py` | Заменить на новый (со встроенным health-сервером) |
| `requirements.txt` | Заменить |
| `render.yaml` | Добавить новый |
| `Procfile` | Удалить (Railway — не нужен) |
| `railway.json` | Удалить (Railway — не нужен) |

---

## Шаг 2 — Зарегистрироваться на Render.com

1. Открыть [render.com](https://render.com)
2. Нажать **"Get Started for Free"**
3. Войти через **GitHub** (кнопка "Continue with GitHub")
4. Разрешить доступ к репозиторию

---

## Шаг 3 — Создать сервис на Render

1. Dashboard → кнопка **"New +"** → **"Web Service"**
2. Выбрать ваш репозиторий `fraukuhni-bot`
3. Заполнить форму:

| Поле | Значение |
|---|---|
| **Name** | fraukuhni-bot |
| **Region** | Frankfurt (EU Central) |
| **Branch** | main |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Plan** | **Free** |

4. Прокрутить вниз → **"Advanced"** → **"Add Environment Variable"**:
   - Key: `TELEGRAM_TOKEN`
   - Value: `ваш_токен_от_BotFather`

5. Нажать **"Create Web Service"**

---

## Шаг 4 — Не давать боту засыпать (cron-job.org)

Render бесплатно засыпает после 15 минут без запросов. Решение:

1. Открыть [cron-job.org](https://cron-job.org) → зарегистрироваться (бесплатно)
2. **"Create cronjob"**
3. Заполнить:
   - **URL:** `https://fraukuhni-bot.onrender.com` (ваш URL из Render Dashboard)
   - **Schedule:** каждые **10 минут** (выбрать "Every 10 minutes")
4. Сохранить

Теперь cron-job.org каждые 10 минут стучится к боту — и он не засыпает.

---

## Шаг 5 — Проверить что всё работает

В Render Dashboard → ваш сервис → вкладка **"Logs"**

Должно появиться:
```
INFO - ✅ БД инициализирована
INFO - ✅ Health-сервер запущен на порту 8080
INFO - 🤖 Бот запущен (polling)...
```

Написать тестовое сообщение в групповой чат:
```
#ФРАУ_КУХНИ Тест +79991234567 город Москва Бюджет 500 000 р
```

В логах должно появиться:
```
INFO - 📋 phone=+79991234567 username=None region=Москва budget=500 000 р
INFO - ✅ Клиент сохранён id=1
```

---

## Итог — стоимость

| Сервис | Цена |
|---|---|
| GitHub | Бесплатно |
| Render.com (Free план) | Бесплатно |
| cron-job.org | Бесплатно |
| **Итого** | **0 ₽/месяц** |
