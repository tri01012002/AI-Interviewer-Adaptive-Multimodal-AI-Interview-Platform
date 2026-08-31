# RAG (Retrieval-Augmented Generation) Pipeline

Knowledge base management and retrieval for AI Interviewer

## Directory Structure

```
rag/
├── documents/               # Knowledge base
│   ├── job_kb/             # Job descriptions
│   ├── technical_kb/       # Technical knowledge
│   └── interview_kb/       # Question bank, rubrics
│
├── ingestion/              # Load documents
│   ├── loaders.py          # PDF, TXT, JSON loaders
│   ├── processors.py       # Clean & normalize
│   └── pipeline.py         # Full pipeline
│
├── chunking/               # Split documents
│   ├── strategies.py       # Semantic, sliding window
│   └── metadata.py         # Add metadata
│
├── embeddings/             # Vectorize text
│   ├── providers.py        # OpenAI, Ollama, Cohere
│   ├── cache.py            # Cache embeddings
│   └── config.py           # Configuration
│
├── vector_store/           # Store vectors
│   ├── supabase_store.py   # pgvector
│   ├── pinecone_store.py   # Pinecone
│   ├── base.py             # Abstract base
│   └── config.py
│
├── retrieval/              # Search documents
│   ├── bm25.py             # Keyword search
│   ├── semantic.py         # Vector search
│   ├── hybrid.py           # Combine both
│   ├── filters.py          # Metadata filters
│   └── config.py
│
├── reranking/              # Rank results
│   ├── providers.py        # Cohere, Local
│   ├── colbert.py          # ColBERT reranker
│   └── config.py
│
├── qa/                     # Quality assurance
│   ├── fact_checker.py     # Verify facts
│   └── grounding.py        # Check grounding
│
└── cache.py                # Cache manager
```

## Core Pipeline

### 1. Ingestion & Processing

```python
from rag.ingestion import RAGIngestionPipeline

pipeline = RAGIngestionPipeline()

# Load documents
docs = await pipeline.load_documents(
    source="./data/job_descriptions.pdf"
)

# Process
processed = await pipeline.process(docs)

# Index
await pipeline.index(processed)
```

### 2. Retrieval

```python
from rag.retrieval import HybridRetriever

retriever = HybridRetriever()

# Search with multiple strategies
results = await retriever.search(
    query="How to optimize YOLO model?",
    top_k=5,
    filters={"skill": "YOLO", "difficulty": "hard"}
)
```

### 3. Reranking

```python
from rag.reranking import CrossEncoderReranker

reranker = CrossEncoderReranker()

# Rerank results
ranked = await reranker.rerank(
    query="...",
    documents=results,
    top_k=3
)
```

### 4. Full RAG Pipeline

```python
from rag import RAG

rag = RAG()

# End-to-end retrieval
context = await rag.retrieve(
    query="Tell me about inference optimization",
    top_k=3
)

# Use in agent
llm_response = await agent.generate_with_context(
    query=question,
    context=context
)
```

## Knowledge Base Structure

### Job Knowledge
```
job_kb/
├── job_descriptions/
│   ├── ai_engineer.md
│   ├── ml_engineer.md
│   └── data_engineer.md
└── skills/
    ├── python_requirements.md
    ├── pytorch_requirements.md
    └── llm_requirements.md
```

### Technical Knowledge
```
technical_kb/
├── tutorials/
│   ├── yolo_tutorial.md
│   ├── rag_tutorial.md
│   └── langgraph_tutorial.md
├── best_practices/
│   ├── model_optimization.md
│   ├── production_deployment.md
│   └── testing_strategies.md
└── examples/
    ├── code_samples/
    └── case_studies/
```

### Interview Knowledge
```
interview_kb/
├── question_bank/
│   ├── easy_questions.json
│   ├── medium_questions.json
│   └── hard_questions.json
├── rubrics/
│   ├── python_rubric.json
│   ├── system_design_rubric.json
│   └── communication_rubric.json
└── evaluation_guidelines/
    └── scoring_guide.md
```

## Configuration

### Embeddings Provider

```python
# .env
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSION=1536
```

### Vector Store

```python
# .env
VECTOR_STORE_PROVIDER=supabase  # or pinecone
VECTOR_STORE_DIMENSION=1536
VECTOR_SIMILARITY_THRESHOLD=0.7
```

### Retrieval Strategy

```python
# .env
RETRIEVAL_TOP_K=5
RETRIEVAL_STRATEGY=hybrid  # semantic, bm25, hybrid
RERANKER_TOP_K=3
RERANKER_THRESHOLD=0.5
```

## Usage Examples

### Load and Index Documents

```python
from rag.ingestion import DocumentLoader
from rag.embeddings import OpenAIEmbeddings
from rag.vector_store import SupabaseVectorStore

# Load
loader = DocumentLoader()
docs = loader.load_pdf("job_description.pdf")

# Embed
embeddings = OpenAIEmbeddings()
embedded_docs = await embeddings.embed_batch([d.content for d in docs])

# Index
store = SupabaseVectorStore()
await store.index(docs, embedded_docs)
```

### Retrieve with Hybrid Search

```python
from rag.retrieval import HybridRetriever

retriever = HybridRetriever()

# Hybrid search combines BM25 (keyword) + semantic (vector)
results = await retriever.hybrid_search(
    query="YOLO object detection inference",
    top_k=5
)

# With filters
results = await retriever.filtered_search(
    query="...",
    filters={
        "skill": "computer_vision",
        "difficulty": ["medium", "hard"]
    }
)
```

### Evaluate Retrieval Quality

```python
from evaluation.rag_eval import RetrievalEvaluator

evaluator = RetrievalEvaluator()

metrics = await evaluator.evaluate(
    golden_dataset="data/golden_dataset.jsonl",
    retriever=retriever
)

print(f"Recall@5: {metrics.recall_at_5}")
print(f"Precision@5: {metrics.precision_at_5}")
```

## Best Practices

1. **Chunk Size**: 512-1024 tokens optimal
2. **Metadata**: Add skill, difficulty, source tags
3. **Reranking**: Always use for quality
4. **Caching**: Cache embeddings to save costs
5. **Testing**: Evaluate on golden dataset
6. **Updates**: Regularly update knowledge base

## Testing

```bash
# RAG tests
pytest tests/unit/rag/ -v

# Evaluation tests
pytest tests/evaluation/rag_eval/ -v

# Integration tests
pytest tests/integration/ -v -k rag
```

## See Also

- [Architecture - RAG Pipeline](../../docs/RAG_GUIDE.md)
- [Evaluation Framework](../evaluation/README.md)
- [LangChain Documentation](https://langchain.com/)
