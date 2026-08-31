# Voice Pipeline Module

Real-time speech I/O for AI Interviewer

## Directory Structure

```
voice/
├── audio/                  # Audio buffer management
│   ├── stream.py          # WebAudio buffer
│   ├── formats.py         # Audio formats
│   └── processors.py      # Audio processing
│
├── vad/                   # Voice Activity Detection
│   ├── base.py            # Abstract base
│   ├── webrtc_vad.py      # WebRTC VAD
│   ├── silero_vad.py      # Silero VAD
│   └── config.py
│
├── stt/                   # Speech-to-Text
│   ├── providers.py       # Whisper, AssemblyAI, Azure
│   ├── streaming.py       # Streaming recognition
│   └── config.py
│
├── tts/                   # Text-to-Speech
│   ├── providers.py       # ElevenLabs, Azure, Google
│   ├── streaming.py       # Streaming synthesis
│   └── config.py
│
├── pipeline/              # Main orchestration
│   ├── voice_pipeline.py  # Coordinator
│   ├── turn_taking.py     # Conversation flow
│   ├── interruption.py    # Handle interruptions
│   └── latency_optimizer.py
│
└── handlers/              # Error handling
    ├── silence_handler.py
    ├── timeout_handler.py
    └── error_handler.py
```

## Real-Time Voice Interview Flow

```
CANDIDATE SPEAKS
      │
      ↓
WebAudio API (Browser)
      │
      ↓
VAD Detection
    / \
  Speech? No → [Wait]
   /
 Yes
  /
 ↓
STT (Streaming)
  ↓
Real-time transcript
  ↓
AGENT PROCESSES
  ↓
LLM generates response
  ↓
TTS (Streaming)
  ↓
PLAY TO CANDIDATE
```

## Core Components

### 1. Voice Activity Detection (VAD)

Detect when candidate is speaking:

```python
from voice.vad import SileroVAD

vad = SileroVAD()

# Detect speech in audio chunk
is_speech = await vad.is_speech(
    audio_chunk=audio_bytes,
    sample_rate=16000
)

# Get speech segments
segments = await vad.get_speech_segments(audio=full_audio)
# segments = [(start_ms, end_ms), ...]
```

### 2. Speech-to-Text (STT)

Convert speech to text in real-time:

```python
from voice.stt import AssemblyAISTT

stt = AssemblyAISTT()

# Streaming transcription
async for chunk in stt.transcribe_stream(audio_stream):
    print(f"Interim: {chunk.transcript}")
    print(f"Confidence: {chunk.confidence}")
    
    if chunk.is_final:
        print(f"Final: {chunk.transcript}")
```

### 3. Text-to-Speech (TTS)

Generate natural speech:

```python
from voice.tts import ElevenLabsTTS

tts = ElevenLabsTTS()

# Generate speech
audio = await tts.synthesize(
    text="Could you explain how RAG works?",
    voice_id="default",
    speed=1.0
)

# Streaming synthesis
async for audio_chunk in tts.synthesize_stream(text_stream):
    await websocket.send_bytes(audio_chunk)
```

### 4. Voice Pipeline (Main Orchestrator)

```python
from voice.pipeline import VoicePipeline

pipeline = VoicePipeline()

# Start recording
await pipeline.start_recording()

# Process in real-time
async for event in pipeline.process():
    if event.type == "vad_detected":
        print("Candidate speaking")
    elif event.type == "transcript_interim":
        print(f"Interim: {event.text}")
    elif event.type == "transcript_final":
        candidate_answer = event.text
        # Send to agent
        agent_response = await agent.process_answer(candidate_answer)
        # Generate speech
        await pipeline.generate_speech(agent_response)
```

## Configuration

```python
# .env

# VAD
VAD_THRESHOLD=0.5
VAD_SAMPLE_RATE=16000

# STT
STT_LANGUAGE=en-US
ASSEMBLY_AI_API_KEY=your-key

# TTS
ELEVEN_LABS_API_KEY=your-key
ELEVEN_LABS_VOICE_ID=default
TTS_SPEED=1.0

# Turn-taking
TURN_TAKING_TIMEOUT=3000  # ms
SILENCE_THRESHOLD=500     # ms
```

## Handling Edge Cases

### Silence Handling

```python
# If candidate goes silent for 3 seconds:
# 1. Repeat question
# 2. Ask for clarification
# 3. Continue to next question

from voice.handlers import SilenceHandler

handler = SilenceHandler(timeout_ms=3000)
```

### Interruption Handling

```python
# If agent is speaking and candidate interrupts:
# 1. Stop TTS
# 2. Process candidate speech
# 3. Update conversation

from voice.handlers import InterruptionHandler

handler = InterruptionHandler()
```

### Backchanneling

```python
# Detect "mm-hmm", "uh-huh" (not real answers)
# Don't evaluate these as answers

from voice.pipeline import TurnTakingManager

turn_manager = TurnTakingManager()
is_backchanneling = await turn_manager.detect_backchanneling("mm-hmm")
```

## Latency Optimization

### Streaming Architecture

- STT: Stream audio chunks as they arrive
- Agent: Process partial transcript
- TTS: Start playing audio before complete

**Target latency: <200ms**

```python
# Parallel processing
while True:
    # Record audio
    audio_chunk = await microphone.read()
    
    # Detect speech (parallel)
    is_speech = vad.is_speech(audio_chunk)
    
    # Transcribe (parallel)
    transcript_chunk = stt.transcribe(audio_chunk)
    
    # Update UI immediately
    await send_interim_transcript(transcript_chunk)
```

## Testing

```bash
# Voice tests
pytest tests/unit/voice/ -v

# Integration tests (requires audio device)
pytest tests/integration/ -v -k voice

# Test with real microphone
pytest tests/integration/voice/ --enable-audio-device
```

## WebRTC Integration (Future)

For video interviews, integrate WebRTC:

```python
from voice.pipeline import WebRTCPipeline

webrtc = WebRTCPipeline()

# Handle WebRTC connection
async def on_track(track):
    while True:
        frame = await track.recv()
        # Process audio
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| High latency | Enable streaming mode, reduce chunk size |
| Poor VAD | Adjust threshold, use better microphone |
| STT errors | Verify API key, check language setting |
| TTS robotic | Use natural voice ID, adjust speed |

## See Also

- [Architecture - Voice Pipeline](../../docs/VOICE_GUIDE.md)
- [Evaluation - Voice Metrics](../evaluation/metrics/voice_metrics.py)
- [WebRTC.org](https://webrtc.org/)
- [Whisper Documentation](https://github.com/openai/whisper)
