#!/usr/bin/env python3
"""
Скрипт для автоматического обновления OAuth-токена Qwen.

Читает ~/.qwen/oauth_creds.json, проверяет срок действия access_token.
Если до истечения осталось менее REFRESH_THRESHOLD секунд — обновляет токен
через Qwen OAuth2 API и перезаписывает файл.

Предназначен для запуска через cron каждый час:
  0 * * * * /usr/bin/python3 /root/e13ocrbot-1/refresh_qwen_token.py >> /var/log/qwen_refresh.log 2>&1
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ── Настройки ──────────────────────────────────────────────────────────────────
CREDS_PATH = Path.home() / ".qwen" / "oauth_creds.json"

QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"

# Обновляем, если до истечения токена осталось менее 2 часов (7200 сек)
REFRESH_THRESHOLD_SEC = 7200


def log(msg: str) -> None:
    """Вывод сообщения с временной меткой."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def load_creds() -> dict:
    """Читает и возвращает содержимое oauth_creds.json."""
    if not CREDS_PATH.exists():
        log(f"ОШИБКА: Файл {CREDS_PATH} не найден")
        sys.exit(1)
    with open(CREDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_creds(creds: dict) -> None:
    """Сохраняет обновлённые credentials в oauth_creds.json."""
    with open(CREDS_PATH, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2, ensure_ascii=False)
    # Ограничиваем доступ только для владельца
    os.chmod(CREDS_PATH, 0o600)
    log(f"Файл {CREDS_PATH} обновлён")


def token_needs_refresh(creds: dict) -> bool:
    """Проверяет, нужно ли обновить токен."""
    expiry_date = creds.get("expiry_date")
    if not expiry_date:
        log("Поле expiry_date отсутствует — обновляем токен")
        return True

    # expiry_date хранится в миллисекундах (как в Qwen CLI)
    now_ms = int(time.time() * 1000)
    remaining_sec = (expiry_date - now_ms) / 1000

    if remaining_sec <= REFRESH_THRESHOLD_SEC:
        log(f"Токен истекает через {remaining_sec:.0f} сек "
            f"(порог: {REFRESH_THRESHOLD_SEC} сек) — обновляем")
        return True
    else:
        log(f"Токен действителен ещё {remaining_sec:.0f} сек "
            f"({remaining_sec / 3600:.1f} ч) — обновление не требуется")
        return False


def refresh_token(refresh_token_value: str) -> dict:
    """
    Обновляет токен через Qwen OAuth2 API.

    Возвращает dict с полями: access_token, refresh_token, token_type,
    expires_in, scope, resource_url.
    """
    data = urllib.parse.urlencode({
        "client_id": QWEN_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
    }).encode("utf-8")

    req = urllib.request.Request(
        QWEN_OAUTH_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # User-Agent обязателен: Alibaba Cloud WAF блокирует Python-urllib
            "User-Agent": "curl/7.81.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")

            # WAF может вернуть HTML вместо JSON — проверяем
            if "text/html" in content_type:
                log("ОШИБКА: API вернул HTML вместо JSON (вероятно WAF-блокировка)")
                log(f"Content-Type: {content_type}, длина: {len(raw)}")
                sys.exit(1)

            if not raw.strip():
                log("ОШИБКА: API вернул пустой ответ")
                log("⚠️ refresh_token, вероятно, недействителен. Выполните: qwen")
                sys.exit(1)

            body = json.loads(raw)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        log(f"ОШИБКА HTTP {e.code}: {error_body}")
        if e.code in (400, 401):
            log("⚠️ refresh_token недействителен. Выполните: qwen")
        sys.exit(1)
    except urllib.error.URLError as e:
        log(f"ОШИБКА сети: {e.reason}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        log(f"ОШИБКА JSON: {e}")
        log(f"Ответ (первые 300 символов): {raw[:300]}")
        sys.exit(1)

    if body.get("status") != "success":
        log(f"ОШИБКА: неожиданный ответ API: {json.dumps(body, ensure_ascii=False)}")
        sys.exit(1)

    return body


def main() -> None:
    log("=== Проверка токена Qwen ===")

    creds = load_creds()

    if not token_needs_refresh(creds):
        return

    rt = creds.get("refresh_token")
    if not rt:
        log("ОШИБКА: refresh_token отсутствует в файле. "
            "Выполните авторизацию: qwen")
        sys.exit(1)

    log("Отправляем запрос на обновление токена...")
    result = refresh_token(rt)

    # Формируем обновлённый файл (как это делает Qwen CLI)
    new_creds = {
        "access_token": result["access_token"],
        "token_type": result.get("token_type", "Bearer"),
        "refresh_token": result["refresh_token"],
        "resource_url": result.get("resource_url", "portal.qwen.ai"),
        # CLI конвертирует expires_in (сек) → expiry_date (мс timestamp)
        "expiry_date": int(time.time() * 1000) + result["expires_in"] * 1000,
    }

    save_creds(new_creds)
    log(f"Токен обновлён! Действителен {result['expires_in'] / 3600:.1f} ч")


if __name__ == "__main__":
    main()

