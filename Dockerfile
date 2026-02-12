FROM python:3.12-slim

# Установка системных зависимостей для Node.js
#RUN apt-get update && apt-get install -y --no-install-recommends \
#    curl \
#    ca-certificates \
#    && rm -rf /var/lib/apt/lists/*

# Установка Node.js 20.x LTS
#RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
#    && apt-get install -y nodejs \
#    && rm -rf /var/lib/apt/lists/*

# Установка Qwen CLI глобально
#RUN npm install -g @qwenai/qwen-cli

# Рабочая директория
WORKDIR /app

# Копирование и установка Python зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода бота
COPY bot.py .

# Переменные окружения передаются через docker-compose или docker run
# Не копируем .env в образ для безопасности!

# Запуск бота
CMD ["python", "bot.py"]

