# Contributing to AI Interviewer

We love your input! We want to make contributing to AI Interviewer as easy and transparent as possible.

## Development Process

We use GitHub to host code, to track issues and feature requests, as well as accept pull requests.

### 1. Fork & Clone

```bash
git clone https://github.com/yourusername/ai-interviewer.git
cd ai-interviewer
git remote add upstream https://github.com/ai-interviewer/platform.git
```

### 2. Setup Development Environment

```bash
# Install dependencies
make install-all

# Create feature branch
git checkout -b feature/your-feature-name
```

### 3. Development Workflow

```bash
# Run tests
make test

# Format code
make format

# Run all checks
make check-all

# Commit changes
git commit -m "feat: add new feature"

# Push to your fork
git push origin feature/your-feature-name
```

### 4. Pull Request

Submit a pull request with:
- Clear description of changes
- Link to related issues
- Test coverage
- Updated documentation

## Code Standards

### Python

- **Formatting**: Black (100 char line length)
- **Imports**: Sorted with isort
- **Typing**: Type hints required
- **Linting**: pylint + flake8
- **Testing**: >80% coverage

```bash
# Auto-format
make format

# Check
make check-all
```

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add new feature
fix: fix bug
docs: update documentation
test: add tests
chore: maintenance
refactor: code refactoring
perf: performance improvement
```

### Naming Conventions

- Classes: `PascalCase`
- Functions/Methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private: prefix with `_`

## Testing

```bash
# Unit tests
make test-unit

# Integration tests
make test-integration

# With coverage
make test-cov

# Specific tests
pytest tests/unit/rag/ -v
```

**Minimum coverage: 80%**

## Documentation

Update documentation for:
- New features
- API changes
- Configuration options
- Architecture updates

```bash
# Build docs
make docs-build

# Serve locally
make docs-serve
```

## Areas to Contribute

### High Priority
- [ ] Complete agent implementations
- [ ] RAG pipeline optimization
- [ ] Voice pipeline robustness
- [ ] Evaluation framework

### Medium Priority
- [ ] Frontend (React)
- [ ] Integrations
- [ ] Performance optimization
- [ ] Documentation

### Future
- [ ] Video interview mode
- [ ] Multilingual support
- [ ] Advanced analytics
- [ ] White-label options

## Reporting Issues

### Bug Reports

Include:
- Python version
- Environment (Docker/Local)
- Reproducible steps
- Expected vs actual behavior
- Logs/error messages

### Feature Requests

Include:
- Use case
- Proposed solution
- Alternative approaches
- Implementation effort estimate

## Questions?

- Open a [Discussion](https://github.com/ai-interviewer/platform/discussions)
- Check [FAQ](docs/FAQ.md)
- Email: dev@ai-interviewer.com

## Code Review

All submissions require review by maintainers:

- Functionality
- Test coverage
- Code quality
- Documentation
- Performance impact

## License

By contributing, you agree that your contributions will be licensed under its MIT License.

## Recognition

Contributors are recognized in:
- README.md
- CONTRIBUTORS.md
- Release notes

---

**Thank you for contributing to AI Interviewer!** 🚀
