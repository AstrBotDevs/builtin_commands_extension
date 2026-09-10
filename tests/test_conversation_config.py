from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from commands import conversation as conversation_module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_type", ["local", "dify", "coze", "dashscope", "deerflow"]
)
@pytest.mark.parametrize("command", ["convs", "del_conv"])
async def test_conversation_commands_respect_runner_config(
    monkeypatch, runner_type, command
):
    config = {
        "platform_settings": {"unique_session": True},
        "provider_settings": {},
        "agent_runner": {"runner_type": runner_type, "config": {}},
    }

    conversation = SimpleNamespace(
        cid="abcd-1234", title="Saved conversation", persona_id="default", updated_at=0
    )
    manager = SimpleNamespace(
        get_conversations=AsyncMock(return_value=[conversation]),
        get_curr_conversation_id=AsyncMock(return_value=conversation.cid),
        delete_conversation=AsyncMock(),
    )
    context = SimpleNamespace(
        get_config=MagicMock(return_value=config),
        conversation_manager=manager,
        persona_manager=SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("default", None, None, False)
            )
        ),
    )
    message = MagicMock(unified_msg_origin="telegram:FriendMessage:123", role="member")
    message.get_group_id.return_value = ""
    message.get_platform_name.return_value = "telegram"
    stop_all = MagicMock()
    remove_state = AsyncMock()
    monkeypatch.setattr(conversation_module.active_event_registry, "stop_all", stop_all)
    monkeypatch.setattr(conversation_module.sp, "remove_async", remove_state)

    await getattr(conversation_module.ConversationCommands(context), command)(message)

    message.set_result.assert_called_once()
    result = message.set_result.call_args.args[0].get_plain_text()
    context.get_config.assert_called_with(umo=message.unified_msg_origin)
    if runner_type == "local":
        remove_state.assert_not_awaited()
        if command == "convs":
            assert "Saved conversation(abcd)" in result
            assert "default" in result
            manager.get_conversations.assert_awaited_once_with(
                message.unified_msg_origin
            )
            stop_all.assert_not_called()
        else:
            manager.delete_conversation.assert_awaited_once_with(
                message.unified_msg_origin, conversation.cid
            )
            stop_all.assert_called_once_with(
                message.unified_msg_origin, exclude=message
            )
    else:
        manager.get_conversations.assert_not_awaited()
        manager.delete_conversation.assert_not_awaited()
        if command == "convs":
            assert "暂不支持" in result
            remove_state.assert_not_awaited()
            stop_all.assert_not_called()
        else:
            remove_state.assert_awaited_once_with(
                scope="umo",
                scope_id=message.unified_msg_origin,
                key=conversation_module.THIRD_PARTY_AGENT_RUNNER_KEY[runner_type],
            )
            stop_all.assert_called_once_with(
                message.unified_msg_origin, exclude=message
            )
