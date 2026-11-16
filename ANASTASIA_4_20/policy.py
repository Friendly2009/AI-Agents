"""Safety and consent gates. No automated cold DMs or scraping."""
from __future__ import annotations
from datetime import date, timedelta
import re

STOP_WORDS = {"стоп", "stop", "отписаться", "не пишите", "удалите мои данные", "удалить данные", "неинтересно", "не интересно", "неактуально", "не актуально"}
YES_WORDS = {"да", "да, согласен", "да согласна", "согласна", "согласен", "ок", "согласен на обработку через ии"}
AI_DECLINE = {"нет", "не согласен", "не согласна"}


def is_stop(text: str) -> bool:
    low = text.strip().lower()
    # Explicit communication withdrawal, never substrings inside «мне интересно».
    if re.search(r"\b(?:не пишите|не пиши|удалите мои данные|удалить данные)\b", low):
        return True
    return bool(re.fullmatch(r"(?:нет[, ]+)?(?:спасибо[, ]+)?(?:стоп|stop|отписаться|неинтересно|не интересно|неактуально|не актуально)(?:[, ]+(?:пожалуйста|спасибо|мне))*[.!?]*", low))


def is_yes(text: str) -> bool:
    low = re.sub(r"[.!?]+$", "", text.strip().lower())
    return low in YES_WORDS or low.startswith("да, соглас")


def is_ai_decline(text: str) -> bool:
    return text.strip().lower().rstrip('.!?') in AI_DECLINE


def is_crm_consent(text: str) -> bool:
    low = text.lower()
    return ("соглас" in low and ("обработ" in low or "данн" in low) and "не соглас" not in low)


def can_outreach(row: dict, today: date | None = None) -> tuple[bool, str]:
    today = today or date.today()
    username = row.get("username", "").strip().lstrip('@')
    if not re.fullmatch(r"[a-zA-Z][\w]{4,31}", username):
        return False, "Некорректный Telegram username"
    if row.get("telegram_contact_consent", "").strip().lower() not in {"yes", "да"}:
        return False, "Нет согласия на контакт в Telegram"
    if row.get("adult_confirmed", "").strip().lower() not in {"yes", "да"}:
        return False, "Не подтверждён совершеннолетний родитель"
    if not row.get("consent_source", "").strip() or len(row["consent_source"].strip()) < 5:
        return False, "Нет проверяемого источника согласия"
    try:
        granted = date.fromisoformat(row.get("consent_date", ""))
    except ValueError:
        return False, "Неверная дата согласия YYYY-MM-DD"
    if not (today - timedelta(days=90) <= granted <= today):
        return False, "Согласие старше 90 дней или датировано будущим"
    if row.get("city", "").strip().lower() not in {"александров", "ярославль"}:
        return False, "Неизвестный город"
    return True, "ok"


def group_trigger(text: str, marker: str) -> bool:
    """Group participation only when a person explicitly tags the agreed marker."""
    return bool(marker and marker.casefold() in text.casefold())
