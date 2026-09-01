# Используем официальный легковесный образ Python 3.12+ (required by nado-protocol SDK)
FROM python:3.12-slim

# Установка системных зависимостей (если понадобятся, например, для компиляции некоторых Python-библиотек)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создаем директорию приложения
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь исходный код
COPY . .

# Убедимся, что директории для логов и данных существуют
RUN mkdir -p data/memory logs

# Устанавливаем PYTHONPATH, чтобы импорты работали корректно
ENV PYTHONPATH=/app/python

# Команда для запуска бота
CMD ["python", "python/main.py"]
