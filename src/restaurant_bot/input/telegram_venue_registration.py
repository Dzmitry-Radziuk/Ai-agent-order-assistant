from __future__ import annotations

import re

from restaurant_bot.domain.models import TelegramEvent
from restaurant_bot.text_normalization import clean_text
from restaurant_bot.venues.codes import valid_code


def registration_input(event: TelegramEvent) -> tuple[str, str]:
    """Разбирает входные данные регистрации заведения."""
    callback = re.sub(r":r\d+$", "", event.callback_data, flags=re.I)
    match = re.fullmatch(r"venue_bind:([A-ZА-ЯЁ0-9]{4,32}):(yes|no)", callback, re.I)
    if match:
        return ("confirm" if match.group(2).lower() == "yes" else "reject"), match.group(1)
    match = re.fullmatch(r"venue_switch:([A-ZА-ЯЁ0-9]{4,32}):(yes|no)", callback, re.I)
    if match:
        return (
            "switch_confirm" if match.group(2).lower() == "yes" else "switch_reject",
            match.group(1),
        )
    text = clean_text(event.text)
    match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?(?:\s+(.+))?", text, re.I)
    if match:
        argument = clean_text(match.group(1))
        if argument.casefold().startswith("review_"):
            return "", ""
        return "start", argument
    match = re.fullmatch(r"/code(?:@[A-Za-z0-9_]+)?(?:\s+(.+))?", text, re.I)
    if match:
        return "code", clean_text(match.group(1))
    match = re.fullmatch(r"код\s+(.+)", text, re.I)
    if match:
        return "code", clean_text(match.group(1))
    if valid_code(text):
        return "code", text
    return "", ""
