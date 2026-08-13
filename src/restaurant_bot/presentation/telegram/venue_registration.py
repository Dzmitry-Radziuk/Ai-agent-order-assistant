from __future__ import annotations

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.presentation.telegram.formatting import escape
from restaurant_bot.venues.contracts import Venue


def new_order_button() -> list[list[Button]]:
    """Возвращает кнопку перехода к созданию новой заявки."""
    return [[Button(text="Новая заявка", callback_data="v2:new")]]


def private_chat_required_reply() -> BotReply:
    """Сообщает об ограничении регистрации личным чатом."""
    return BotReply(text="Подключить заведение можно только в личном чате с ботом.")


def rejected_reply() -> BotReply:
    """Сообщает об отмене подключения."""
    return BotReply(text="Подключение отменено.")


def already_connected_reply(venue_name: str) -> BotReply:
    """Сообщает о уже подключённом заведении."""
    return BotReply(
        text=f"Вы уже подключены к заведению:\n«{escape(venue_name)}»",
        rows=new_order_button(),
    )


def directory_error_reply() -> BotReply:
    """Сообщает о временной ошибке справочника."""
    return BotReply(
        text="Не удалось проверить код заведения.\n\nПопробуйте ещё раз через несколько секунд."
    )


def code_not_found_reply() -> BotReply:
    """Сообщает о ненайденном коде заведения."""
    return BotReply(
        text=(
            "Код заведения не найден.\n\n"
            "Проверьте код или запросите новую invite-ссылку у менеджера АвтоСнаб."
        )
    )


def code_conflict_reply() -> BotReply:
    """Сообщает о неоднозначном коде заведения."""
    return BotReply(
        text="Код привязки неоднозначен.\n\nОбратитесь к менеджеру АвтоСнаб за новой invite-ссылкой."
    )


def sync_failure_reply() -> BotReply:
    """Сообщает об ошибке сохранения подключения."""
    return BotReply(
        text=(
            "Не удалось сохранить подключение.\n\n"
            "Попробуйте ещё раз или обратитесь к менеджеру АвтоСнаб."
        )
    )


def access_disabled_reply() -> BotReply:
    """Сообщает пользователю об отключённом доступе."""
    return BotReply(
        text=(
            "⛔ <b>Доступ к заведению отключён</b>\n\n"
            "Обратитесь к ответственному сотруднику вашего заведения."
        )
    )


def confirmation(venue: Venue) -> BotReply:
    """Формирует карточку подтверждения заведения."""
    return BotReply(
        text=f"Вы хотите подключиться к заведению:\n«{escape(venue.name)}»?",
        rows=[
            [Button(text="Да, подключить", callback_data=f"venue_bind:{venue.code}:yes")],
            [Button(text="Нет", callback_data=f"venue_bind:{venue.code}:no")],
        ],
    )


def switch_confirmation(current_name: str, venue: Venue) -> BotReply:
    """Формирует подтверждение смены заведения."""
    return BotReply(
        text=(
            "Сейчас Вы подключены к заведению:\n"
            f"«{escape(current_name)}».\n\n"
            "Подключиться вместо него к заведению:\n"
            f"«{escape(venue.name)}»?"
        ),
        rows=[
            [Button(text="Да, изменить", callback_data=f"venue_switch:{venue.code}:yes")],
            [Button(text="Отмена", callback_data=f"venue_switch:{venue.code}:no")],
        ],
    )


def binding_success_reply(venue: Venue, *, already_same: bool) -> BotReply:
    """Формирует ответ после успешной привязки."""
    text = (
        f"Вы уже подключены к заведению:\n«{escape(venue.name)}»"
        if already_same
        else (
            "<i>Вы подключены к заведению:</i>\n"
            f"«{escape(venue.name)}»\n\nТеперь Вы можете создавать заявки в этом чате."
        )
    )
    return BotReply(text=text, rows=new_order_button())


def not_bound_reply() -> BotReply:
    """Формирует инструкцию для непривязанного пользователя."""
    return BotReply(
        text=(
            "Ваше заведение ещё не подключено.\n\n"
            "Отправьте код заведения или перейдите по invite-ссылке, "
            "полученной от менеджера АвтоСнаб."
        )
    )
