"""Provider-neutral voice interfaces and deterministic test providers."""
from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Protocol
from voice.contracts import AudioFormat, STTEvent

class VADProvider(Protocol):
    async def detect(self, audio: bytes, audio_format: AudioFormat) -> bool: ...

class STTProvider(Protocol):
    async def start_session(self, utterance_id: str, audio_format: AudioFormat) -> None: ...
    async def send_audio(self, utterance_id: str, audio: bytes) -> AsyncIterator[STTEvent]: ...
    async def end_utterance(self, utterance_id: str) -> AsyncIterator[STTEvent]: ...
    async def close(self) -> None: ...

class TTSProvider(Protocol):
    async def stream(self, text: str) -> AsyncIterator[bytes]: ...
    async def close(self) -> None: ...

def get_provider(provider_name: str, provider_type: str, **kwargs) -> VADProvider | STTProvider | TTSProvider:
    """Factory function for provider selection."""
    if provider_type == "vad":
        if provider_name == "fake":
            return FakeVADProvider()
        raise ValueError(f"Unknown VAD provider: {provider_name}")
    
    if provider_type == "stt":
        if provider_name == "fake":
            return FakeSTTProvider()
        elif provider_name == "assemblylabs":
            from voice.real_providers import AssemblyAISTTProvider
            return AssemblyAISTTProvider(**kwargs)
        raise ValueError(f"Unknown STT provider: {provider_name}")
    
    if provider_type == "tts":
        if provider_name == "fake":
            return FakeTTSProvider()
        elif provider_name == "elevenlabs":
            from voice.real_providers import ElevenLabsTTSProvider
            return ElevenLabsTTSProvider(**kwargs)
        raise ValueError(f"Unknown TTS provider: {provider_name}")
    
    raise ValueError(f"Unknown provider type: {provider_type}")

class FakeVADProvider:
    async def detect(self, audio: bytes, audio_format: AudioFormat) -> bool:
        return bool(audio)

class FakeSTTProvider:
    def __init__(self, final_text: str = "I used Python in production.") -> None:
        self.final_text = final_text
        self.started: list[str] = []
        self.audio: dict[str, bytearray] = {}

    async def start_session(self, utterance_id: str, audio_format: AudioFormat) -> None:
        self.started.append(utterance_id)
        self.audio[utterance_id] = bytearray()

    async def send_audio(self, utterance_id: str, audio: bytes) -> AsyncIterator[STTEvent]:
        self.audio.setdefault(utterance_id, bytearray()).extend(audio)
        yield STTEvent(utterance_id=utterance_id, text="I used Python", is_final=False)

    async def end_utterance(self, utterance_id: str) -> AsyncIterator[STTEvent]:
        yield STTEvent(utterance_id=utterance_id, text=self.final_text, is_final=True)

    async def close(self) -> None:
        self.audio.clear()

class FakeTTSProvider:
    def __init__(self, chunk_size: int = 8) -> None:
        self.chunk_size = chunk_size

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        encoded = text.encode("utf-8")
        for index in range(0, len(encoded), self.chunk_size):
            yield encoded[index:index + self.chunk_size]

    async def close(self) -> None:
        return None
