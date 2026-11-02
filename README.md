# 🤖 AI Interviewer — Adaptive Multimodal AI Interview Platform

**Autonomous Voice Interview & Candidate Evaluation Platform**

Transform recruitment with AI-powered adaptive interviews that automatically adjust questions based on candidate responses, leverage knowledge bases, evaluate answers objectively, and generate comprehensive reports.

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue)](https://github.com/ai-interviewer/platform)
[![Python](https://img.shields.io/badge/Python-3.11+-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-teal)](https://fastapi.tiangolo.com/)

---

## 🎯 Features

### ✨ Adaptive Interview Strategy
- **Real-time skill detection** from candidate responses
- **Dynamic question generation** based on competency gaps
- **Intelligent follow-ups** for deep dives into weak areas
- Learns candidate knowledge on-the-fly

### 🎤 Multimodal Input/Output
- **Text Mode** - Chat-based asynchronous interviews
- **Voice Mode** - Real-time speech with STT/TTS
- **Video Mode** - Webcam-based interviews (coming soon)
- Same core agent, different interfaces

### 🧠 Smart Question Generation
- Retrieves relevant questions from knowledge base
- Generates context-aware follow-ups
- Adapts difficulty based on performance
- Avoids redundant questioning

### 📊 Objective Scoring
- Evidence-based evaluation (not gut feeling)
- Structured rubrics for each skill
- Confidence scoring (87% confident → strong signal)
- Fairness-auditable (no voice/accent bias)

### 🔧 Real-time RAG Integration
- Retrieves job requirements in real-time
- Dynamic question bank with difficulty levels
- Answer rubrics for objective evaluation
- Feedback generation with evidence

### 🤖 Browser Agent (Automation)
- Auto-upload interview reports
- Schedule follow-up interviews
- Update candidate status
- Safe, auditable actions only

---

## 🏗️ Architecture

```
AI INTERVIEWER PLATFORM
        │
    ┌───┴───┐
    ↓       ↓
[TEXT]   [VOICE]  → Same AGENT CORE (LangGraph)
    │       ↓
    └───┬───┘
        ↓
    ┌─────────────────────┐
    │  LangGraph Agent    │
    │  • Interview Planner│
    │  • Question Gen     │
    │  • Evaluator        │
    └────────┬────────────┘
             │
     ┌───────┼───────┐
     ↓       ↓       ↓
    RAG  Voice   Tools
     │       │       │
     └───────┼───────┘
             ↓
    ┌─────────────────────┐
    │ Candidate Report    │
    │ • Score: 82/100     │
    │ • Skills breakdown  │
    │ • Feedback          │
    └─────────────────────┘
```

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed design.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Node.js 18+ (for frontend)
- Git

### Clone & Setup

```bash
git clone https://github.com/ai-interviewer/platform.git
cd ai-interviewer

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Setup environment
cp .env.example .env
# The default deterministic mode needs no external AI API key.
```

Development admin bootstrap is disabled by default. To opt in locally, set
`ENABLE_DEV_ADMIN_BOOTSTRAP=true` and provide private `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_PASSWORD` values; never use shared/default credentials or enable it in
production. Local API binding defaults to `127.0.0.1`.

### Offline AI development

By default, `LLM_PROVIDER=none` uses the platform's deterministic analysis and question
rules, and `EMBEDDING_PROVIDER=none` keeps retrieval lexical-only. Evaluations identify
themselves with `evaluator_type=deterministic`; these scores and feedback are development
signals, not real AI evaluations.

For fixed provider-interface fixtures, set `LLM_PROVIDER=mock`. Follow-up questions include
`[MOCK QUESTION - NOT AI-GENERATED]`, interview state reports `question_source=mock`, and
evaluations report `evaluator_type=mock` with an explicit synthetic-score disclaimer.
Selecting `LLM_PROVIDER=openai` or `EMBEDDING_PROVIDER=openai` requires `OPENAI_API_KEY`;
invalid provider configuration fails explicitly instead of switching to offline behavior.
Live provider tests remain opt-in and are not needed for normal development or tests.

To run the API with local dependencies and no AI credentials:

```powershell
docker compose up -d redis
$env:LLM_PROVIDER = "none"
$env:EMBEDDING_PROVIDER = "none"
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Alternatively, `docker compose up -d postgres redis api` starts the API using the same
offline defaults. No OpenAI, Groq, DeepSeek, or other AI provider credentials are required.

### Run with Docker Compose

```bash
docker-compose up -d

# Verify services
curl http://localhost:8000/health
```

Services:
- **API**: http://localhost:8000
- **Frontend**: http://localhost:3000
- **Docs**: http://localhost:8000/docs

### Start Interview (Manual)

```bash
python -m apps.api.main

# In another terminal:
curl -X POST http://localhost:8000/api/v1/interview/start \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_id": "test-123",
    "position": "AI Engineer",
    "mode": "text"
  }'
```

### Answer submission and idempotency

Answer requests require an authenticated owner (or authorized administrator) and a client-stable
`turn_id`. Repeating the same `turn_id` with the same answer replays the stored interview state
after the first request completes; only one durable turn and its evaluation are created. Requests
that arrive while that interview is still locked receive `409 Conflict` and can be retried with the
same ID and answer. Reusing a `turn_id` with a different answer, or submitting a new turn after
completion, also receives `409 Conflict`. Role-forbidden operations remain `403 Forbidden`;
inaccessible interviews return `404 Not Found`; unavailable distributed locking returns
`503 Service Unavailable`.

The returned `question_source` identifies deterministic, mock, configured-provider, or
deterministic-fallback question generation. Result and report evaluation metadata carries the
corresponding `evaluator_type`; `mock` and `deterministic` results must not be represented as
real AI assessments.

---

## 📚 Documentation

- [PITCH.md](PITCH.md) - Executive summary for investors
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - Detailed technical design
- [API.md](docs/API.md) - API documentation
- [AGENT_DESIGN.md](docs/AGENT_DESIGN.md) - LangGraph workflow
- [RAG_GUIDE.md](docs/RAG_GUIDE.md) - RAG pipeline guide
- [VOICE_GUIDE.md](docs/VOICE_GUIDE.md) - Voice pipeline guide
- [EVALUATION.md](docs/EVALUATION.md) - Evaluation framework
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) - Production deployment
- [SECURITY.md](docs/SECURITY.md) - Security & fairness
- [BACKEND_READINESS.md](docs/BACKEND_READINESS.md) - Verified backend scope and offline provider setup

---

## 📁 Project Structure

```
ai-interviewer/
├── apps/                  # Applications
│   ├── api/              # FastAPI backend
│   ├── web/              # React frontend
│   └── worker/           # Background jobs
├── agents/               # LangGraph agents
│   ├── interview_agent/  # Main orchestrator
│   ├── question_agent/   # Question generation
│   ├── evaluation_agent/ # Answer evaluation
│   └── browser_agent/    # Automation
├── rag/                  # RAG pipeline
├── voice/                # Voice I/O pipeline
├── evaluation/           # Evaluation framework
├── integrations/         # 3rd-party integrations
├── config/               # Configuration
├── schemas/              # Data types
├── utils/                # Utilities
├── tests/                # Test suites
├── docker/               # Docker files
├── docs/                 # Documentation
└── README.md
```

---

## 🔧 Core Technologies

### Backend
- **FastAPI** - High-performance async web framework
- **LangGraph** - Agent state machine & workflow
- **Pydantic** - Data validation & settings
- **SQLAlchemy** - Database ORM

### AI/ML
- **OpenAI / Anthropic** - LLM providers
- **Whisper** - Speech-to-text
- **ElevenLabs** - Text-to-speech
- **Cohere / LangChain** - RAG & retrieval

### Database
- **Supabase** - PostgreSQL + pgvector (Auth, DB, Storage)
- **Redis** - Caching & task queue
- **Celery** - Background job processing

### Observability
- **LangWatch** - LLM observability
- **Sentry** - Error tracking
- **Prometheus** - Metrics
- **DataDog** - APM (optional)

### Frontend
- **React** - UI framework
- **Next.js** - Full-stack framework
- **TailwindCSS** - Styling
- **WebRTC** - Voice/video communication

---

## 🎯 Development Workflow

### Setup Development Environment

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install

# Create database
make db-migrate

# Seed sample data
make db-seed
```

### Run Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# With coverage
pytest --cov=agents --cov=rag --cov=voice

# Watch mode
ptw
```

### Lint & Format

```bash
# Lint
pylint apps agents

# Format
black .
isort .

# Security check
bandit -r apps agents

# All checks
make lint
```

### Start Development Servers

```bash
# Terminal 1: Backend
make run-api

# Terminal 2: Frontend
make run-web

# Terminal 3: Workers
make run-worker

# Terminal 4: Evaluation
make run-eval
```

---

## 💾 Environment Variables

See `.env.example` for full list:

```env
# API
API_HOST=0.0.0.0
API_PORT=8000

# Database
DATABASE_URL=postgresql://...
SUPABASE_URL=...
SUPABASE_KEY=...

# LLM
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...

# Voice
ELEVEN_LABS_API_KEY=...
ASSEMBLY_AI_API_KEY=...

# Observability
LANGWATCH_API_KEY=...
SENTRY_DSN=...

# Feature Flags
LAUNCH_DARKLY_KEY=...
```

---

## 🚀 Deployment

### Docker Compose (Development)

```bash
docker-compose up -d
```

### Kubernetes (Production)

```bash
# Build images
docker build -t ai-interviewer:latest .

# Deploy
kubectl apply -f kubernetes/

# Monitor
kubectl logs -f deployment/api-deployment
```

See [DEPLOYMENT.md](docs/DEPLOYMENT.md) for full guide.

---

## 🧪 Evaluation & Testing

### Synthetic Interview Cases

```bash
# Generate 1000 synthetic cases
python -m evaluation.datasets.synthetic.generator --count 1000

# Run regression suite
python -m evaluation.regression.runner

# Compare with baseline
python -m evaluation.regression.comparator --baseline v1.0
```

### RAG Evaluation

```bash
# Evaluate retrieval quality
python -m evaluation.rag_eval.retrieval_eval --golden_dataset data/golden/

# Check answer grounding
python -m evaluation.rag_eval.qa_eval
```

---

## 📊 Metrics & Monitoring

### Dashboard

Access monitoring dashboard at http://localhost:8000/dashboard

### Key Metrics

**Interview Quality:**
- Question relevance (target: >90%)
- Skill coverage (target: >80%)
- Completion rate (target: >95%)

**RAG Performance:**
- Retrieval recall@5 (target: >85%)
- Reranking precision (target: >90%)

**Agent Performance:**
- Tool accuracy (target: >95%)
- Hallucination rate (target: <2%)

**Voice Quality:**
- WER (Word Error Rate, target: <5%)
- Turn-taking latency (target: <200ms)

---

## 🔐 Security & Fairness

### Fairness Guardrails
- No voice/accent bias (text-based scoring)
- Evidence-based evaluation only
- Audit trail for all decisions
- Transparent scoring rubrics

### Security
- Permission-based action validation
- Encrypted audio storage
- PII masking in logs
- SQL injection prevention (SQLAlchemy)
- Rate limiting on API endpoints

See [SECURITY.md](docs/SECURITY.md) for details.

---

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md)

### Coding Standards
- Black code formatting
- mypy type checking
- pylint linting
- >80% test coverage

### Pull Request Process
1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing`)
5. Open Pull Request

---

## 📄 License

License - see [LICENSE](LICENSE)

---

## 🙋 Support & Contact

- **Issues**: [GitHub Issues](https://github.com/ai-interviewer/platform/issues)
- **Discussions**: [GitHub Discussions](https://github.com/ai-interviewer/platform/discussions)
- **Email**: tringuyen.01012002@gmail.com
- **Website**: https://ai-interviewer.com

---

## 📈 Roadmap

- [ ] **Q1 2025**: MVP (Text + Voice modes)
- [ ] **Q2 2025**: Video mode, Browser agent
- [ ] **Q3 2025**: Multilingual support
- [ ] **Q4 2025**: Industry-specific templates
- [ ] **Q1 2026**: Predictive hiring analytics

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed roadmap.

---

## 🎖️ Acknowledgments

Built with ❤️ using:
- [LangChain](https://langchain.com/)
- [Supabase](https://supabase.com/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [React](https://react.dev/)

---

**"Replace 4-6 hour manual interviews with 30-minute adaptive AI interviews. Better decisions, lower costs, fairer hiring."**

[⭐ Star us on GitHub](https://github.com/tri01012002/AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform)
