#!/usr/bin/env python3
"""
E13 OCR Bot — Telegram бот для распознавания текста с изображений.

Использует Qwen Portal Vision API для извлечения текста из фотографий
и документов-изображений, отправленных в чат.
"""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
from io import BytesIO
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Загрузка переменных окружения
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")

# Путь к файлу с OAuth-креденшелами Qwen (монтируется из ~/.qwen/ хоста)
QWEN_OAUTH_CREDS_PATH: str = os.getenv("QWEN_OAUTH_CREDS_PATH", "/app/oauth_creds.json")

# ---------------------------------------------------------------------------
# Настройки API
# ---------------------------------------------------------------------------
QWEN_API_URL: str = "https://portal.qwen.ai/v1/chat/completions"
QWEN_MODEL_ID: str = "vision-model"
API_TIMEOUT: int = 30  # секунд
MAX_TOKENS: int = 4096

# ---------------------------------------------------------------------------
# Промпт для Vision API
# ---------------------------------------------------------------------------
VISION_PROMPT: str = (
    "Извлеки весь видимый текст с изображения.\n\n"
    "Требования:\n"
    "1. Сохрани ВСЁ что видишь - комментарии, кнопки, метки, цифры, ссылки\n"
    "2. Используй markdown для структуры (заголовки, списки, выделение)\n"
    "3. Особое внимание к цифрам - они должны быть точными\n"
    "4. Сохрани порядок чтения (сверху вниз, слева направо)\n"
    "5. Если текст нечёткий - напиши [неразборчиво]\n\n"
    "Верни только извлечённый текст в markdown, без своих комментариев."
)

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("e13ocrbot")


# =========================================================================
# Работа с Qwen Vision API
# =========================================================================

def get_qwen_token() -> str:
    """
    Получает актуальный OAuth-токен Qwen.

    Читает access_token из oauth_creds.json (монтируется из ~/.qwen/).
    Принудительно синхронизирует файловую систему перед чтением,
    чтобы получить актуальное содержимое файла.
    """
    try:
        # Принудительно синхронизируем буферы ФС, чтобы увидеть обновления с хоста
        os.sync()

        with open(QWEN_OAUTH_CREDS_PATH, "r", encoding="utf-8", buffering=1) as f:
            creds = json.load(f)
        token = creds.get("access_token", "")
        if token:
            logger.debug("OAuth-токен прочитан из %s", QWEN_OAUTH_CREDS_PATH)
            return token
        else:
            logger.error("Поле access_token пустое в %s", QWEN_OAUTH_CREDS_PATH)
            raise ValueError("access_token отсутствует в файле")
    except FileNotFoundError:
        logger.error("Файл %s не найден", QWEN_OAUTH_CREDS_PATH)
        raise
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Ошибка чтения %s: %s", QWEN_OAUTH_CREDS_PATH, exc)
        raise


async def call_vision_api(image_base64: str) -> str:
    """
    Отправляет base64-изображение в Qwen Vision API и возвращает
    распознанный текст.

    Args:
        image_base64: Строка с изображением в формате base64 (без префикса).

    Returns:
        Распознанный текст или сообщение об ошибке.

    Raises:
        httpx.TimeoutException: При превышении таймаута.
        httpx.HTTPStatusError: При HTTP-ошибке от API.
    """
    payload: dict = {
        "model": QWEN_MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": VISION_PROMPT,
                    },
                ],
            }
        ],
        "max_tokens": MAX_TOKENS,
    }

    headers: dict = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_qwen_token()}",
    }

    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        response = await client.post(
            QWEN_API_URL,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()

    data: dict = response.json()

    # Извлекаем текст из ответа
    try:
        text: str = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        logger.error("Неожиданный формат ответа API: %s", data)
        raise ValueError("Не удалось разобрать ответ API") from exc

    return text


# =========================================================================
# Вспомогательные функции
# =========================================================================

async def download_and_encode(file_obj, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Скачивает файл из Telegram и кодирует его в base64.

    Args:
        file_obj: Объект файла Telegram (PhotoSize / Document).
        context: Контекст бота.

    Returns:
        Строка base64 без префикса.
    """
    tg_file = await context.bot.get_file(file_obj.file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# =========================================================================
# Обработчики команд и сообщений
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start — приветствие и инструкция."""
    welcome_text: str = (
        "👋 *Привет!*\n\n"
        "Я бот для распознавания текста с изображений.\n\n"
        "📸 *Как пользоваться:*\n"
        "1. Отправь мне фотографию или изображение-документ\n"
        "2. Подожди несколько секунд\n"
        "3. Получи распознанный текст в формате Markdown\n\n"
        "💡 *Совет:* Для лучшего качества отправляй изображение "
        "как документ (без сжатия).\n\n"
        "Поддерживаемые форматы: JPEG, PNG, WebP, GIF."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик фотографий (сжатых Telegram).

    Берёт фото максимального размера, отправляет в Vision API,
    возвращает распознанный текст.
    """
    # Берём фото максимального разрешения
    photo = update.message.photo[-1]
    await _process_image(update, context, photo)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик документов-изображений (оригинальное качество).

    Проверяет MIME-тип, скачивает файл, отправляет в Vision API.
    """
    document = update.message.document

    # Проверяем, что документ — изображение
    mime: Optional[str] = document.mime_type
    if not mime or not mime.startswith("image/"):
        await update.message.reply_text(
            "⚠️ Пожалуйста, отправьте изображение (JPEG, PNG, WebP, GIF)."
        )
        return

    await _process_image(update, context, document)


async def _process_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_obj,
) -> None:
    """
    Общая логика обработки изображения: скачивание, вызов API, ответ.

    Args:
        update: Объект обновления Telegram.
        context: Контекст бота.
        file_obj: PhotoSize или Document из Telegram.
    """
    # Отправляем индикатор обработки
    processing_msg = await update.message.reply_text("⏳ Обрабатываю...")

    try:
        # Скачиваем и кодируем изображение
        image_b64: str = await download_and_encode(file_obj, context)
        logger.info(
            "Изображение получено, размер base64: %d символов",
            len(image_b64),
        )

        # Вызываем Vision API
        result_text: str = await call_vision_api(image_b64)

        # Отправляем результат
        reply: str = f"📝 *Текст:*\n\n{result_text}"

        # Telegram ограничивает длину сообщения — разбиваем если нужно
        if len(reply) <= 4096:
            await processing_msg.edit_text(reply, parse_mode="Markdown")
        else:
            # Разбиваем на части по 4096 символов
            await processing_msg.edit_text(
                "📝 *Текст (разбит на части из-за длины):*",
                parse_mode="Markdown",
            )
            for i in range(0, len(result_text), 4000):
                chunk: str = result_text[i : i + 4000]
                await update.message.reply_text(chunk)

    except httpx.TimeoutException:
        logger.error("Таймаут при запросе к Vision API")
        await processing_msg.edit_text(
            "⏱ Превышено время ожидания ответа от сервера. "
            "Попробуйте ещё раз позже."
        )

    except httpx.HTTPStatusError as exc:
        status_code: int = exc.response.status_code
        logger.error("HTTP ошибка %d: %s", status_code, exc.response.text)

        if status_code == 401:
            error_msg = "🔑 Ошибка авторизации. Проверьте токен API."
        elif status_code == 429:
            error_msg = "🚦 Превышен лимит запросов. Попробуйте позже."
        elif status_code >= 500:
            error_msg = "🔧 Сервер временно недоступен. Попробуйте позже."
        else:
            error_msg = f"❌ Ошибка сервера (код {status_code}). Попробуйте позже."

        await processing_msg.edit_text(error_msg)

    except ValueError as exc:
        logger.error("Ошибка разбора ответа API: %s", exc)
        await processing_msg.edit_text(
            "❌ Не удалось обработать ответ от сервера. Попробуйте ещё раз."
        )

    except Exception as exc:
        logger.exception("Непредвиденная ошибка: %s", exc)
        await processing_msg.edit_text(
            "❌ Произошла непредвиденная ошибка. Попробуйте позже."
        )


# =========================================================================
# Запуск бота
# =========================================================================

def main() -> None:
    """Точка входа — создание и запуск бота."""
    # Проверяем наличие обязательных токенов
    if not TELEGRAM_TOKEN:
        logger.critical("Не задана переменная окружения TELEGRAM_TOKEN")
        sys.exit(1)

    if not os.path.exists(QWEN_OAUTH_CREDS_PATH):
        logger.critical(
            "Файл %s не найден. Убедитесь, что он монтирован через docker-compose",
            QWEN_OAUTH_CREDS_PATH,
        )
        sys.exit(1)

    # Проверяем, что файл содержит валидный токен
    try:
        get_qwen_token()
        logger.info("Токен Qwen успешно загружен из %s", QWEN_OAUTH_CREDS_PATH)
    except Exception as exc:
        logger.critical("Не удалось загрузить токен из %s: %s", QWEN_OAUTH_CREDS_PATH, exc)
        sys.exit(1)

    logger.info("Запуск E13 OCR Bot...")

    # Создаём приложение бота
    app: Application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # Graceful shutdown: корректное завершение при SIGINT / SIGTERM
    logger.info("Бот запущен. Ожидание сообщений...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=[signal.SIGINT, signal.SIGTERM],
    )

    logger.info("Бот остановлен.")


if __name__ == "__main__":
    main()
