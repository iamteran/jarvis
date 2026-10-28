import unittest
from unittest.mock import Mock

from jarvis.telegram_bridge import TelegramBridge


class TestTelegramBridge(unittest.TestCase):
    def setUp(self):
        self.reply = Mock(return_value="Ready.")
        self.bridge = TelegramBridge("fake-token", "42", self.reply)
        self.bridge._send = Mock()

    def _update(self, user_id, text, chat_type="private"):
        return {
            "message": {
                "text": text,
                "chat": {"id": int(user_id), "type": chat_type},
                "from": {"id": int(user_id)},
            }
        }

    def test_authorized_private_message_reaches_jarvis(self):
        self.bridge._handle_update(self._update(42, "What time is it?"))
        self.reply.assert_called_once_with("What time is it?")
        self.bridge._send.assert_called_once_with(42, "Ready.")

    def test_unauthorized_user_cannot_send_commands(self):
        self.bridge._handle_update(self._update(99, "open notepad"))
        self.reply.assert_not_called()
        self.bridge._send.assert_called_once()

    def test_group_messages_are_ignored(self):
        self.bridge._handle_update(self._update(42, "open notepad", "group"))
        self.reply.assert_not_called()
        self.bridge._send.assert_not_called()

    def test_id_command_does_not_execute_jarvis_command(self):
        self.bridge._handle_update(self._update(99, "/id"))
        self.reply.assert_not_called()
        self.bridge._send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
