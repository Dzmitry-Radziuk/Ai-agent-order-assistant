from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def test_reply_callbacks_receive_one_ui_revision_suffix() -> None:
    """Проверяет, что ответ callback receive один ui ревизия суффикс."""
    reply = BotReply(
        text="Черновик",
        rows=[
            [
                Button(text="Черновик", callback_data="v2:back"),
                Button(text="Без действия", callback_data=""),
            ]
        ],
    )

    UpdateOrchestrator._attach_ui_revision(reply, 9)
    UpdateOrchestrator._attach_ui_revision(reply, 9)

    assert reply.rows[0][0].callback_data == "v2:back:r9"
    assert reply.rows[0][1].callback_data == ""
