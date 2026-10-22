"""OpenAI-compatible continuous conversation controller."""

from core.openai import OpenAIManager
from core.external_conversation import ExternalConversationController


class OpenAIConversationController(ExternalConversationController):
    """Manages multi-turn conversation for OpenAI-compatible services.

    一次唤醒一个服务端会话：进入连续对话时开新会话 ID，退出（再见 / 超时 / 被打断）时丢掉。
    """

    CONFIG_PREFIX = "openai"
    BACKEND_NAME = "OpenAI"
    LOG_MODULE = "OpenAI Conv"
    WAKEUP_SOURCE = "openai"
    MANAGER = OpenAIManager

    async def start(self, wake_position: int | None = None):
        if not self.active:
            OpenAIManager.begin_conversation()
        await super().start(wake_position)

    def stop(self):
        was_active = self.active
        super().stop()
        if was_active:
            OpenAIManager.end_conversation()
