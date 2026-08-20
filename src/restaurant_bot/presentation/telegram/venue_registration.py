"""Формирует Telegram-представление «venue registration»."""

from __future__ import annotations

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.presentation.telegram.formatting import escape, heading
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
            f"⛔ {heading('Доступ к заведению отключён')}\n\n"
            "Обратитесь к ответственному сотруднику вашего заведения."
        )
    )


def multiple_venues_reply() -> BotReply:
    """Объясняет, почему нужно выбрать одно из нескольких заведений."""
    return BotReply(
        text=(
            "У вас есть доступ к нескольким заведениям, но текущее заведение не выбрано.\n\n"
            "Откройте invite-ссылку нужного заведения или введите его код.\n"
            "Если доступы указаны ошибочно, обратитесь к ответственному сотруднику."
        )
    )


def venue_status_reply(
    *,
    current_name: str = "",
    available_names: list[str] | None = None,
    access_disabled: bool = False,
) -> BotReply:
    """Отвечает, к какому заведению привязан пользователь сейчас."""
    names = [name for name in (available_names or []) if name]
    if access_disabled:
        return BotReply(
            text=(
                f"🏢 {heading('Подключение к заведению')}\n\n"
                "Текущая привязка отключена. Обратитесь к ответственному сотруднику "
                "заведения или откройте новую invite-ссылку."
            )
        )
    if current_name and len(names) <= 1:
        return BotReply(
            text=(
                f"🏢 {heading('Текущее заведение')}\n\n"
                f"Вы подключены к заведению:\n<b>{escape(current_name)}</b>."
            ),
            rows=[
                [Button(text="Показать черновик", callback_data="v2:cartpage:0")],
                [Button(text="Посмотреть статусы заявок", callback_data="v2:orders")],
                [Button(text="Новая заявка", callback_data="v2:new")],
            ],
        )
    if current_name:
        lines = [
            f"🏢 {heading('Текущее заведение')}",
            "",
            f"Сейчас выбрано заведение:\n<b>{escape(current_name)}</b>.",
            "",
            "У вас также есть доступ к:",
        ]
        for name in names:
            if name != current_name:
                lines.append(f"• {escape(name)}")
        lines += ["", "Чтобы переключиться, откройте invite-ссылку нужного заведения."]
        return BotReply(text="\n".join(lines))
    if names:
        return BotReply(
            text=(
                f"🏢 {heading('Доступные заведения')}\n\n"
                "Сейчас выбрано несколько заведений:\n"
                + "\n".join(f"• {escape(name)}" for name in names)
                + "\n\nОткройте invite-ссылку нужного заведения, чтобы выбрать его."
            )
        )
    return BotReply(
        text=(
            f"🏢 {heading('Подключение к заведению')}\n\n"
            "Вы пока не подключены ни к одному заведению."
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
