import aiohttp
from typing import Optional

class SessionManager:
    """
    Global aiohttp ClientSession manager.
    Prevents socket exhaustion and TIME_WAIT leaks by sharing a single session.
    """
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    async def get(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession()
        return cls._session
        
    @classmethod
    async def close(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None
