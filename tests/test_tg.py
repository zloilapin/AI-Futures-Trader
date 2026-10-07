import os
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from dotenv import load_dotenv
from services.telegram_service import TelegramService

import pytest

@pytest.mark.asyncio
async def test_broadcast():
    load_dotenv()
    tg = TelegramService()
    
    # Mock network call to prevent spamming live Telegram channel during automated unit testing
    mock_response = MagicMock()
    mock_response.status = 200
    mock_post_context = AsyncMock()
    mock_post_context.__aenter__.return_value = mock_response
    mock_post_context.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.post.return_value = mock_post_context

    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        res = await tg.broadcast_to_channel("Тестовое сообщение от AI-Trader")
        # If credentials configured, it sends via mock session and returns True
        if tg.bot_token and tg.public_channel_id:
            assert res is True
            assert mock_session.post.called is True

if __name__ == "__main__":
    asyncio.run(test_broadcast())
