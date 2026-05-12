# Direct Analytics Dashboard

Дашборд аналитики рекламных кампаний Яндекс.Директ для NEDICOM.

## Что делает

- Таблица кампаний: статус, показы, клики, CTR, расход за 30 дней
- Анализ кампаний через Claude AI с историей
- Произвольные вопросы Claude в контексте статистики
- Парольная защита

## Стек

- Python 3.10, Flask, Gunicorn
- Яндекс.Директ API v5
- Anthropic Claude API (claude-sonnet-4-6)

## Запуск локально

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env  # заполни переменные
venv/bin/python app.py
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `DIRECT_TOKEN` | OAuth токен Яндекс.Директ |
| `ANTHROPIC_API_KEY` | Ключ Anthropic API |
| `DASHBOARD_PASSWORD` | Пароль для входа |
| `SECRET_KEY` | Случайная строка для Flask сессий |
| `HTTPS_PROXY` | Прокси для Anthropic API (опционально) |

## Деплой

Автодеплой через GitHub Actions при пуше в `main`:

```bash
git push origin main
```

Или вручную на сервере:

```bash
cd /home/forge/direct.nedicom.ru
git pull origin main
systemctl restart direct-analytics
```

## Сервер

- URL: https://direct.nedicom.ru
- Systemd: `direct-analytics.service`
- Порт: 8002 (за nginx)
