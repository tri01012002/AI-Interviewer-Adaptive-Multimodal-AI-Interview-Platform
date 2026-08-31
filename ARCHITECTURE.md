# 🏗️ AI Interviewer - Detailed Architecture

**Complete technical architecture guide for AI Interviewer platform**

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Core Components](#core-components)
3. [Data Flow](#data-flow)
4. [LangGraph Agent](#langgraph-agent)
5. [RAG Pipeline](#rag-pipeline)
6. [Voice Pipeline](#voice-pipeline)
7. [API Layer](#api-layer)
8. [Database Schema](#database-schema)
9. [Observability](#observability)
10. [Deployment](#deployment)
11. [Security & Fairness](#security--fairness)

---

## System Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER (Frontend)                           │
│                                                                           │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│   │  Text Mode   │  │  Voice Mode  │  │  Video Mode  │                  │
│   │   (React)    │  │   (WebRTC)   │  │  (WebRTC)    │                  │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                  │
│          │                 │                 │                          │
│          └─────────────────┼─────────────────┘                          │
│                            │                                             │
│                  REST / WebSocket                                        │
│                            │                                             │
└────────────────────────────┼─────────────────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────────────────┐
│                      API LAYER (FastAPI)                                 │
│                            │                                             │
│              ┌─────────────┴──────────────┐                             │
│              ↓                            ↓                             │
│        ┌──────────────┐          ┌─────────────────┐                  │
│        │ HTTP Routes  │          │ WebSocket       │                  │
│        │              │          │ Connection      │                  │
│        │ POST /       │          │ Manager         │                  │
│        │ interview    │          │                 │                  │
│        │              │          │ Real-time       │                  │
│        │ GET /        │          │ streaming       │                  │
│        │ evaluation   │          │                 │                  │
│        └──────┬───────┘          └────────┬────────┘                  │
│               │                          │                            │
│               └──────────────┬───────────┘                            │
│                              ↓                                         │
│                    ┌──────────────────┐                               │
│                    │  Middleware      │                               │
│                    │  • Auth          │                               │
│                    │  • Rate Limit    │                               │
│                    │  • Logging       │                               │
│                    └────────┬─────────┘                               │
│                             ↓                                         │
└─────────────────────────────┼───────────────────────────────────────┘
                              │
┌─────────────────────────────┼───────────────────────────────────────┐
│                    AGENT ORCHESTRATION LAYER                         │
│                             │                                        │
│              ┌──────────────┴───────────────┐                       │
│              ↓                              ↓                       │
│         ┌─────────────┐            ┌────────────────┐              │
│         │ Interview   │            │ Browser Agent  │              │
│         │ Orchestrator│            │ (Optional)     │              │
│         └──────┬──────┘            └────────┬───────┘              │
│                │                           │                       │
│                ├─────────────────────────────┘                      │
│                │                                                    │
│                ↓                                                    │
│        ┌─────────────────────────────┐                             │
│        │   LangGraph State Machine   │                             │
│        │                             │                             │
│        │  ┌─────────────────────┐   │                             │
│        │  │ InterviewState      │   │                             │
│        │  │ {                   │   │                             │
│        │  │   candidate_id      │   │                             │
│        │  │   position          │   │                             │
│        │  │   conversation      │   │                             │
│        │  │   skills            │   │                             │
│        │  │   scores            │   │                             │
│        │  │   topics_covered    │   │                             │
│        │  │   interview_stage   │   │                             │
│        │  │   remaining_time    │   │                             │
│        │  │ }                   │   │                             │
│        │  └──────────┬──────────┘   │                             │
│        └─────────────┼──────────────┘                              │
│                      ↓                                             │
│        ┌─────────────────────────────┐                            │
│        │   Agent Workflow Nodes      │                            │
│        │                             │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 1. Load State        │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 2. Analyze Candidate │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 3. Determine Goal    │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 4. Retrieve Knowledge│  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 5. Generate Question │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 6. Validate Question │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 7. Ask Candidate     │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 8. Receive Answer    │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 9. Analyze Answer    │  │                            │
│        │  │  (RAG + Evaluation)  │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 10. Score Answer     │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 11. Update State     │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ↓               │                            │
│        │  ┌──────────────────────┐  │                            │
│        │  │ 12. Route (Continue? │  │                            │
│        │  │       or End?)       │  │                            │
│        │  └──────────┬───────────┘  │                            │
│        │             ├──┬────────┐  │                            │
│        │             │  │        │  │                            │
│        │            Yes No       │  │                            │
│        │             │  │        │  │                            │
│        │             ↓  ↓        │  │                            │
│        │  ┌────────────────────┐│  │                            │
│        │  │ Continue Interview ││  │                            │
│        │  │ or Final Eval      ││  │                            │
│        │  └──────┬─────────────┘│  │                            │
│        │         │              │  │                            │
│        │         └──────────┬───┘  │                            │
│        └────────────────────┼──────┘                             │
└────────────────────────────┼────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────┐
│                   SUPPORTING SERVICES LAYER                     │
│                             │                                  │
│    ┌──────────────┬─────────┼───────────┬──────────────┐       │
│    │              │         │           │              │       │
│    ↓              ↓         ↓           ↓              ↓       │
│  ┌────────┐  ┌───────┐  ┌──────┐  ┌────────┐  ┌────────────┐  │
│  │ RAG    │  │Voice  │  │LLM   │  │Config  │  │Tools       │  │
│  │Pipeline│  │Pipeline│  │      │  │        │  │(Browser,   │  │
│  │        │  │        │  │      │  │        │  │Evaluation) │  │
│  │        │  │        │  │      │  │        │  │            │  │
│  │• Embed │  │• VAD   │  │•OPENAI│ │•Settings│  │• Permission│  │
│  │• Retrieve  │• STT │  │•Claude│ │•Secrets │  │  Validator │  │
│  │• Rerank│  │• TTS  │  │•Ollama│ │•Logging │  │• Action    │  │
│  │• QA    │  │• Turn │  │       │  │         │  │  Executor  │  │
│  └────────┘  │       │  └──────┘  └────────┘  └────────────┘  │
│              │ taking│                                         │
│              └───────┘                                         │
└────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────┐
│                    DATA & PERSISTENCE LAYER                     │
│                             │                                  │
│    ┌──────────────┬─────────┼───────────┬──────────────┐       │
│    │              │         │           │              │       │
│    ↓              ↓         ↓           ↓              ↓       │
│  ┌────────┐  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────────┐  │
│  │Supabase│  │Vector│  │Cache   │  │Storage │  │Queue     │  │
│  │(Auth,  │  │DB    │  │(Redis) │  │(Audio, │  │(Celery)  │  │
│  │DB,     │  │      │  │        │  │Reports)│  │          │  │
│  │Files)  │  │      │  │        │  │        │  │          │  │
│  └────────┘  └──────┘  └────────┘  └────────┘  └──────────┘  │
└────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────┐
│                  OBSERVABILITY & MONITORING LAYER               │
│                             │                                  │
│    ┌──────────────┬─────────┼───────────┬──────────────┐       │
│    │              │         │           │              │       │
│    ↓              ↓         ↓           ↓              ↓       │
│  ┌────────┐  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────────┐  │
│  │LangWatch│  │Sentry│  │Prometheus│ │DataDog │  │Feature   │  │
│  │(LLM    │  │(Error│  │(Metrics)  │ │(APM)  │  │Flags    │  │
│  │Traces) │  │Track)│  │           │ │       │  │(LD)     │  │
│  └────────┘  └──────┘  └────────────┘ └────────┘  └──────────┘  │
└────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. Interview Orchestrator

**Responsibility**: Coordinate the entire interview lifecycle

```python
class InterviewOrchestrator:
    """Main entry point for interview execution"""
    
    async def start_interview(self, interview_config: InterviewConfig) -> InterviewSession:
        # 1. Load candidate profile
        # 2. Fetch job description
        # 3. Initialize interview state
        # 4. Start agent loop
        # 5. Manage voice/text I/O
        # 6. Handle interruptions
        # 7. Generate final report
        
    async def handle_answer(self, answer: str) -> Question:
        # 1. Save answer to conversation
        # 2. Trigger RAG analysis
        # 3. Trigger evaluation
        # 4. Update state
        # 5. Decide next question
        # 6. Return question
        
    async def end_interview(self) -> CandidateReport:
        # 1. Finalize evaluation
        # 2. Generate report
        # 3. Save to database
        # 4. Trigger post-processing (email, webhook)
```

### 2. LangGraph State Machine

**InterviewState**:
```python
class InterviewState(TypedDict):
    # Identifiers
    candidate_id: str
    interview_id: str
    
    # Interview context
    position: str
    job_description: str
    job_requirements: List[str]
    interview_config: InterviewConfig
    
    # Conversation
    conversation: List[ConversationTurn]  # Q&A pairs
    current_round: int
    
    # Candidate skills assessment
    skills: Dict[str, SkillAssessment]  # {skill: {score, confidence, evidence}}
    topics_covered: List[str]
    topics_missing: List[str]
    
    # Evaluation
    evaluations: List[AnswerEvaluation]
    skill_scores: Dict[str, float]
    overall_score: float
    
    # Metadata
    interview_stage: str  # "introduction", "technical", "system_design", "final"
    remaining_time: int  # seconds
    total_time_elapsed: int
    follow_up_depth: int  # How deep we're going into a topic
    
    # State flags
    interview_complete: bool
    needs_follow_up: bool
    should_continue: bool
```

**Node Functions**:
```python
def load_state(state: InterviewState) -> InterviewState:
    """Load candidate profile, job description, etc."""
    
def analyze_candidate(state: InterviewState) -> InterviewState:
    """Analyze what we know about candidate so far"""
    
def determine_next_objective(state: InterviewState) -> InterviewState:
    """Decide what to ask next based on gaps"""
    
def retrieve_knowledge(state: InterviewState) -> InterviewState:
    """Query RAG for relevant questions, rubrics, context"""
    
def generate_question(state: InterviewState) -> InterviewState:
    """Generate adaptive question using LLM + RAG context"""
    
def validate_question(state: InterviewState) -> InterviewState:
    """Ensure question is relevant, fair, not redundant"""
    
def ask_candidate(state: InterviewState) -> InterviewState:
    """Send question to candidate via voice/text"""
    
def receive_answer(state: InterviewState) -> InterviewState:
    """Collect answer from candidate"""
    
def analyze_answer(state: InterviewState) -> InterviewState:
    """Use RAG + LLM to analyze answer"""
    
def score_answer(state: InterviewState) -> InterviewState:
    """Score answer using evaluation rubrics"""
    
def update_state(state: InterviewState) -> InterviewState:
    """Update skills, topics, scores based on answer"""
    
def route_next_step(state: InterviewState) -> Literal["continue", "end"]:
    """Decide: continue interview or end?"""
    
def finalize_evaluation(state: InterviewState) -> InterviewState:
    """Generate final report"""
```

---

## RAG Pipeline

### Architecture

```
                    INGESTION PHASE
                          │
                ┌─────────┴─────────┐
                ↓                   ↓
        ┌────────────────┐  ┌────────────────┐
        │ Job Knowledge  │  │ Technical KB   │
        │ • JD           │  │ • Tutorials    │
        │ • Skills       │  │ • Best practices
        │ • Rubrics      │  │ • Examples     │
        └────────┬───────┘  └────────┬───────┘
                 │                    │
                 └────────┬───────────┘
                          ↓
                  ┌─────────────────┐
                  │ Document Load   │
                  │ & Processing    │
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │ Chunking        │
                  │ Strategy        │
                  │ • Semantic      │
                  │ • Sliding window│
                  │ • Fixed size    │
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │ Add Metadata    │
                  │ • Skill tags    │
                  │ • Difficulty    │
                  │ • Source type   │
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │ Embed           │
                  │ • OpenAI        │
                  │ • Ollama        │
                  │ • Cohere        │
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │ Vector Store    │
                  │ • pgvector      │
                  │ • Pinecone      │
                  └────────┬────────┘
                           ↓
                           
                    RETRIEVAL PHASE
                           │
            ┌──────────────┴──────────────┐
            ↓                             ↓
      Candidate Answer            Query Template
            │                             │
            └──────────┬──────────────────┘
                       ↓
              ┌─────────────────┐
              │ Multi-Strategy  │
              │ Retrieval       │
              │ • BM25          │
              │ • Semantic      │
              │ • Hybrid        │
              └────────┬────────┘
                       ↓
              ┌─────────────────┐
              │ Retrieval       │
              │ Results         │
              │ (Top-K chunks)  │
              └────────┬────────┘
                       ↓
              ┌─────────────────┐
              │ Reranking       │
              │ • Cross-encoder │
              │ • ColBERT       │
              └────────┬────────┘
                       ↓
              ┌─────────────────┐
              │ Ranked Results  │
              │ (Top-K best)    │
              └────────┬────────┘
                       ↓
              ┌─────────────────┐
              │ Context Quality │
              │ Check           │
              │ • Relevance     │
              │ • Completeness  │
              └────────┬────────┘
                       ↓
                       
              ┌─────────────────────────┐
              │ LLM Agent               │
              │ (With RAG context)      │
              │                         │
              │ ① Generate question     │
              │ ② Evaluate answer       │
              │ ③ Generate feedback     │
              └────────┬────────────────┘
                       ↓
              ┌─────────────────┐
              │ Response        │
              │ • Question      │
              │ • Evaluation    │
              │ • Feedback      │
              └─────────────────┘
```

### RAG Components Detail

**1. Embeddings Layer**
```python
class EmbeddingProvider:
    """Abstract base for embeddings"""
    
    async def embed_text(self, text: str) -> List[float]:
        """Generate embedding"""
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Batch embed for efficiency"""

# Implementations
class OpenAIEmbeddings(EmbeddingProvider): pass
class OllamaEmbeddings(EmbeddingProvider): pass
```

**2. Vector Store Layer**
```python
class VectorStore:
    """Abstract base for vector storage"""
    
    async def index(self, documents: List[Document], embeddings: List[List[float]]) -> None:
        """Index documents"""
    
    async def search(self, query_embedding: List[float], top_k: int = 5) -> List[Document]:
        """Search similar documents"""
    
    async def delete(self, doc_ids: List[str]) -> None:
        """Delete documents"""

# Implementations
class SupabaseVectorStore(VectorStore): pass  # pgvector
class PineconeVectorStore(VectorStore): pass
```

**3. Retrieval Strategy**
```python
class RetrievalStrategy:
    """Multi-strategy retrieval"""
    
    async def semantic_search(self, query: str, top_k: int) -> List[Document]:
        """Semantic similarity"""
    
    async def bm25_search(self, query: str, top_k: int) -> List[Document]:
        """Keyword matching"""
    
    async def hybrid_search(self, query: str, top_k: int) -> List[Document]:
        """Combine both"""
    
    async def filtered_search(self, query: str, filters: Dict) -> List[Document]:
        """Search with metadata filters (skill, difficulty)"""
```

**4. Reranking**
```python
class Reranker:
    """Rerank retrieval results"""
    
    async def rerank(self, query: str, documents: List[Document], top_k: int) -> List[Document]:
        """Rerank using cross-encoder or ColBERT"""
    
    async def score_relevance(self, query: str, document: Document) -> float:
        """Score single document"""
```

---

## Voice Pipeline

### Real-Time Voice Architecture

```
CANDIDATE SPEAKS
      │
      ↓
┌──────────────────┐
│ Audio Buffer     │ ← WebAudio API captures
└──────┬───────────┘
       │
       ↓
┌──────────────────┐
│ VAD (Voice       │ ← Silero VAD or WebRTC
│ Activity         │   Detects if speech detected
│ Detection)       │
└──────┬───────────┘
       │
       ├─ No speech ─→ [Silence handling]
       │
       └─ Speech detected
            │
            ↓
       ┌──────────────────┐
       │ Audio Chunking   │ ← Chunk into 250ms windows
       └──────┬───────────┘
              │
              ↓
       ┌──────────────────┐
       │ STT Provider     │ ← Whisper or AssemblyAI
       │ (Streaming)      │   Real-time transcription
       └──────┬───────────┘
              │
              ↓
       ┌──────────────────┐
       │ Interim Results  │ ← Show "thinking..." to user
       └──────┬───────────┘
              │
              ├─ Interruption detected ─→ [Stop Agent]
              │
              └─ Speech ends (timeout)
                     │
                     ↓
              ┌──────────────────┐
              │ Final Transcript │
              └──────┬───────────┘
                     │
                     ↓
              ┌──────────────────┐
              │ AGENT PROCESSES  │
              │ ANSWER           │
              └──────┬───────────┘
                     │
                     ↓
              ┌──────────────────┐
              │ LLM generates    │
              │ next question    │
              └──────┬───────────┘
                     │
                     ↓
              ┌──────────────────┐
              │ TTS Provider     │ ← ElevenLabs or Azure
              │ (Streaming)      │   Real-time synthesis
              └──────┬───────────┘
                     │
                     ↓
              ┌──────────────────┐
              │ Audio Buffer     │ ← Queue for playback
              └──────┬───────────┘
                     │
                     ↓
              ┌──────────────────┐
              │ Play to Candidate│ ← WebAudio playback
              └──────────────────┘
```

### Key Components

**1. Voice Activity Detection**
```python
class VADProvider:
    """Detect speech presence"""
    
    async def is_speech(self, audio_chunk: bytes) -> bool:
        """Is this chunk speech?"""
    
    async def get_speech_segments(self, audio: bytes) -> List[Tuple[int, int]]:
        """Get start/end timestamps of speech"""

# Implementations
class SileroVAD(VADProvider): pass
class WebRTCVAD(VADProvider): pass
```

**2. STT (Speech-to-Text)**
```python
class STTProvider:
    """Convert speech to text"""
    
    async def transcribe(self, audio: bytes, language: str = "en") -> Transcript:
        """One-shot transcription"""
    
    async def transcribe_stream(self, audio_stream: AsyncIterator[bytes]) -> AsyncIterator[TranscriptChunk]:
        """Streaming transcription"""
    
    # Implementations
    # • Whisper (OpenAI) - high quality
    # • AssemblyAI - streaming + real-time
    # • Google Cloud Speech - enterprise
```

**3. TTS (Text-to-Speech)**
```python
class TTSProvider:
    """Convert text to speech"""
    
    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        """Generate audio from text"""
    
    async def synthesize_stream(self, text_stream: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Streaming synthesis"""

# Implementations
class ElevenLabsSST(TTSProvider): pass  # Natural sounding
class AzureSpeechST(TTSProvider): pass  # Enterprise
class PollyTTS(TTSProvider): pass       # AWS
```

**4. Turn-Taking Manager**
```python
class TurnTakingManager:
    """Manage conversation turn-taking"""
    
    async def detect_end_of_turn(self, transcript: str, audio: bytes) -> bool:
        """Determine if candidate finished speaking"""
    
    async def handle_interruption(self, user_text: str) -> str:
        """Handle mid-sentence interruption"""
    
    async def detect_backchanneling(self, transcript: str) -> bool:
        """Detect "mm-hmm", "uh-huh" etc."""
```

---

## API Layer

### REST Endpoints

```
POST /api/v1/interview/start
├─ Request
│  ├─ candidate_id: str
│  ├─ position: str
│  ├─ mode: Literal["text", "voice", "video"]
│  └─ config: InterviewConfig
├─ Response
│  └─ interview_session: InterviewSession {
│     ├─ interview_id
│     ├─ websocket_url
│     └─ initial_message

POST /api/v1/interview/{interview_id}/answer
├─ Request
│  ├─ answer: str
│  ├─ timestamp: int
│  └─ duration: int (for audio)
├─ Response
│  └─ question_response: QuestionResponse {
│     ├─ question: str
│     ├─ question_id: str
│     └─ follow_up: bool

GET /api/v1/interview/{interview_id}/status
├─ Response
│  └─ status: InterviewStatus {
│     ├─ stage: str
│     ├─ progress: float (0-1)
│     └─ time_remaining: int

POST /api/v1/interview/{interview_id}/end
├─ Response
│  └─ result: InterviewResult {
│     ├─ report: CandidateReport
│     └─ evaluation: CandidateEvaluation

GET /api/v1/interview/{interview_id}/report
├─ Response
│  └─ report: CandidateReport {
│     ├─ overall_score
│     ├─ skills: Dict[str, SkillScore]
│     ├─ strengths: List[str]
│     ├─ weaknesses: List[str]
│     └─ recommendation: str
```

### WebSocket Communication

```
CANDIDATE INITIATES
      │
      ↓
POST /api/v1/interview/start
      │
      ↓
UPGRADE TO WebSocket
      │
      ↓
Client → {type: "answer", text: "..."}
      ↓
Server → {type: "question", text: "..."}
      ↓
Client → {type: "answer", text: "..."}
      ↓
...
      ↓
Server → {type: "interview_end", report: {...}}
      ↓
Close connection
```

---

## Database Schema

### Core Tables

```sql
-- Users & Candidates
TABLE users {
    id: UUID PK
    email: STRING UNIQUE
    name: STRING
    role: ENUM("candidate", "recruiter", "admin")
    created_at: TIMESTAMP
}

TABLE candidates {
    id: UUID PK
    user_id: UUID FK users
    profile: JSONB
    created_at: TIMESTAMP
}

-- Interview Configuration
TABLE jobs {
    id: UUID PK
    title: STRING
    description: TEXT
    required_skills: JSONB
    interview_config: JSONB
    created_at: TIMESTAMP
}

-- Interview Execution
TABLE interviews {
    id: UUID PK
    candidate_id: UUID FK candidates
    job_id: UUID FK jobs
    status: ENUM("pending", "in_progress", "completed", "failed")
    mode: ENUM("text", "voice", "video")
    state: JSONB  -- Serialized InterviewState
    started_at: TIMESTAMP
    ended_at: TIMESTAMP
    duration: INT  -- seconds
    created_at: TIMESTAMP
}

TABLE conversation_messages {
    id: UUID PK
    interview_id: UUID FK interviews
    role: ENUM("assistant", "candidate")
    content: TEXT
    timestamp: TIMESTAMP
    duration: INT  -- for audio
    metadata: JSONB  -- confidence, etc
}

-- Evaluation & Scoring
TABLE answer_evaluations {
    id: UUID PK
    interview_id: UUID FK interviews
    message_id: UUID FK conversation_messages
    score: FLOAT
    skill_name: STRING
    evidence: TEXT
    confidence: FLOAT
    rubric_used: JSONB
    created_at: TIMESTAMP
}

TABLE candidate_scores {
    id: UUID PK
    interview_id: UUID FK interviews
    skill_name: STRING
    score: FLOAT (0-5)
    confidence: FLOAT (0-1)
    evidence_count: INT
    last_updated: TIMESTAMP
}

TABLE interview_reports {
    id: UUID PK
    interview_id: UUID FK interviews
    candidate_id: UUID FK candidates
    overall_score: FLOAT
    technical_score: FLOAT
    communication_score: FLOAT
    problem_solving_score: FLOAT
    strengths: JSONB
    weaknesses: JSONB
    recommendation: ENUM("strong_yes", "yes", "maybe", "no")
    confidence: FLOAT
    report_text: TEXT
    generated_at: TIMESTAMP
}

-- RAG Knowledge Base
TABLE documents {
    id: UUID PK
    job_id: UUID FK jobs
    doc_type: ENUM("job_description", "technical_guide", "question", "rubric")
    title: STRING
    content: TEXT
    metadata: JSONB  -- skills, difficulty, source
    embedding: vector(1536)  -- pgvector
    created_at: TIMESTAMP
}

TABLE question_bank {
    id: UUID PK
    job_id: UUID FK jobs
    question_text: TEXT
    category: STRING
    difficulty: ENUM("easy", "medium", "hard")
    skills: JSONB
    expected_answer: TEXT
    rubric: JSONB
    follow_ups: JSONB
    created_at: TIMESTAMP
}

TABLE scoring_rubrics {
    id: UUID PK
    job_id: UUID FK jobs
    skill_name: STRING
    criteria: JSONB
    score_levels: JSONB  -- {1: "poor", 2: "fair", 3: "good", 4: "very good", 5: "excellent"}
    evidence_required: JSONB
    created_at: TIMESTAMP
}

-- Audit & Observability
TABLE audit_logs {
    id: UUID PK
    interview_id: UUID FK interviews
    action: STRING
    actor: STRING  -- "agent", "candidate", "system"
    details: JSONB
    timestamp: TIMESTAMP
}

TABLE agent_trace_logs {
    id: UUID PK
    interview_id: UUID FK interviews
    trace_id: STRING  -- LangWatch trace ID
    node_name: STRING
    duration: INT  -- ms
    input_tokens: INT
    output_tokens: INT
    cost: FLOAT
    timestamp: TIMESTAMP
}

-- Vector Search (using pgvector)
INDEX documents_embedding_idx ON documents USING ivfflat (embedding vector_cosine_ops)
```

---

## Observability

### LangWatch Integration

```python
from langwatch import observe
from langwatch.types import LLMEvent, ToolCall

@observe(name="interview_agent")
async def run_interview_agent(state: InterviewState) -> InterviewState:
    """Track entire interview lifecycle"""
    
    # Automatically tracks:
    # - LLM calls (model, tokens, cost)
    # - Tool calls (name, duration, success)
    # - Errors and hallucinations
    # - End-to-end latency
```

### Metrics Collection

```python
class MetricsCollector:
    """Collect business & technical metrics"""
    
    async def record_question_metrics(self, question: Question):
        """Track question quality"""
        # • relevance_score
        # • difficulty_level
        # • time_to_generate
        # • user_satisfaction
    
    async def record_answer_metrics(self, answer: str, evaluation: AnswerEvaluation):
        """Track answer quality"""
        # • answer_length
        # • confidence_score
        # • skill_match
        # • evaluation_time
    
    async def record_interview_metrics(self, interview: Interview):
        """Track interview metrics"""
        # • completion_rate
        # • total_duration
        # • skill_coverage
        # • overall_score
        # • candidate_satisfaction
```

### Dashboards

```
1. Interview Quality Dashboard
   - Questions per interview
   - Average score
   - Skill coverage
   - Completion rate

2. Agent Performance Dashboard
   - Tool call accuracy
   - Hallucination rate
   - Average latency
   - Token usage

3. RAG Performance Dashboard
   - Retrieval recall@k
   - Reranking precision
   - Average latency
   - Document coverage

4. Voice Quality Dashboard
   - WER (Word Error Rate)
   - Turn-taking latency
   - STT/TTS quality
   - Interruption handling

5. Business Metrics
   - Candidates processed
   - Avg time per interview
   - Cost per interview
   - Candidate satisfaction
```

---

## Deployment

### Container Strategy

```
Docker Images:
├── api:latest          # FastAPI backend
├── worker:latest       # Celery workers
├── web:latest          # React frontend
└── evaluation:latest   # Evaluation runner

Docker Compose:
├── api (port 8000)
├── worker (background jobs)
├── web (port 3000)
├── redis (cache/queue)
├── postgres (database)
└── pgvector (vector store)
```

### Kubernetes (Optional)

```yaml
Deployments:
├── api-deployment (3 replicas)
├── worker-deployment (5 replicas)
├── web-deployment (2 replicas)
├── redis-deployment
└── postgres-deployment

Services:
├── api-service (LoadBalancer)
├── web-service (LoadBalancer)
└── internal services (ClusterIP)

ConfigMaps:
└── app-config

Secrets:
├── api-keys
├── db-credentials
└── llm-secrets
```

---

## Security & Fairness

### Fairness Guardrails

```python
class FairnessAuditor:
    """Ensure fair, unbiased interviews"""
    
    async def audit_decision(self, decision: EvaluationDecision) -> AuditReport:
        """Check for bias"""
        # 1. Extract protected attributes (voice, name, etc)
        # 2. Check if decision correlates with these
        # 3. Flag suspicious patterns
        # 4. Generate audit report
    
    async def mask_protected_info(self, transcript: str) -> str:
        """Remove identifying information"""
        # • Names → [CANDIDATE_NAME]
        # • Locations → [LOCATION]
        # • Dates → [DATE]
```

### Permission System

```python
class ActionValidator:
    """Validate browser agent actions"""
    
    ALLOWED_ACTIONS = [
        "search_candidate",
        "view_candidate_profile",
        "upload_interview_report",
        "schedule_follow_up",
        "send_feedback_email"
    ]
    
    FORBIDDEN_ACTIONS = [
        "delete_candidate",
        "modify_score",
        "reject_candidate",
        "access_other_interviews"
    ]
    
    async def validate(self, action: Action) -> bool:
        """Check if action is allowed"""
```

---

## Development Workflow

```
Local Development:
├── Docker Compose (all services)
├── Hot reload (API + Web)
├── Local Supabase (optional)
└── Test data seeds

CI/CD:
├── Tests (unit, integration, e2e)
├── Linting (pylint, black, isort)
├── Security scan (bandit, safety)
├── Build Docker images
└── Push to registry

Staging:
├── Full environment copy
├── Real API keys (limited)
├── Production-like database
└── Manual testing

Production:
├── Blue-green deployment
├── Gradual rollout (canary)
├── Health checks
├── Auto-rollback on failure
```

---

This architecture is designed to be **production-ready**, **scalable**, **fair**, and **observable**.

Next: Implementation guide coming soon!
