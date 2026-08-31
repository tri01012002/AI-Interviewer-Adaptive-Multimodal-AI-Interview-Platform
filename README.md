# 🤖 AI Interviewer — Adaptive Multimodal AI Interview Platform

**Autonomous Voice Interview & Candidate Evaluation Platform**

Transform recruitment with AI-powered adaptive interviews that automatically adjust questions based on candidate responses, leverage knowledge bases, evaluate answers objectively, and generate comprehensive reports.

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue)](https://github.com/ai-interviewer/platform)
[![License: All Rights Reserved](https://shields.io)](#-license--copyright)
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
# Edit .env with your API keys
```

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

MIT License - see [LICENSE](LICENSE)

---

## 🙋 Support & Contact

- **Issues**: [GitHub Issues](https://github.com/ai-interviewer/platform/issues)
- **Discussions**: [GitHub Discussions](https://github.com/ai-interviewer/platform/discussions)
- **Email**: support@ai-interviewer.com
- **Website**: https://ai-interviewer.com

---

## 📈 Roadmap

- [ ] **Q3 2024**: MVP (Text + Voice modes)
- [ ] **Q4 2024**: Video mode, Browser agent
- [ ] **Q1 2025**: Multilingual support
- [ ] **Q2 2025**: Industry-specific templates
- [ ] **Q3 2025**: Predictive hiring analytics

See [PROJECT.md](docs/PROJECT.md) for detailed roadmap.

---

## 🎖️ Acknowledgments

Built with ❤️ using:
- [LangChain](https://langchain.com/)
- [Supabase](https://supabase.com/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [React](https://react.dev/)

---

**"Replace 4-6 hour manual interviews with 30-minute adaptive AI interviews. Better decisions, lower costs, fairer hiring."**

[⭐ Star us on GitHub](https://github.com/ai-interviewer/platform)
