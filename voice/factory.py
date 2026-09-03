"""Voice provider factory and initialization utilities."""
from __future__ import annotations
from voice.providers import VADProvider, STTProvider, TTSProvider, get_provider
from config import settings

def create_providers() -> tuple[VADProvider, STTProvider, TTSProvider]:
    """Create voice providers based on configuration."""
    
    # VAD provider (always fake for now)
    vad = get_provider(settings.VAD_PROVIDER, "vad")
    
    # STT provider
    stt_kwargs = {
        "api_key": settings.ASSEMBLY_AI_API_KEY,
        "model": settings.STT_MODEL,
        "language": settings.STT_LANGUAGE,
        "timeout_seconds": settings.STT_TIMEOUT_SECONDS,
        "max_retries": settings.STT_MAX_RETRIES,
    }
    stt = get_provider(settings.STT_PROVIDER, "stt", **stt_kwargs)
    
    # TTS provider
    tts_kwargs = {
        "api_key": settings.ELEVEN_LABS_API_KEY,
        "voice_id": settings.ELEVEN_LABS_VOICE_ID,
        "model": settings.TTS_MODEL,
        "timeout_seconds": settings.TTS_TIMEOUT_SECONDS,
        "max_retries": settings.TTS_MAX_RETRIES,
    }
    tts = get_provider(settings.TTS_PROVIDER, "tts", **tts_kwargs)
    
    return vad, stt, tts
