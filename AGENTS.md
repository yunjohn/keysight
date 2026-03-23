# Repository Guidelines

## Project Structure & Module Organization
This repository is currently a minimal Python workspace. At the moment, only local environment files are present:

- `.venv/`: local virtual environment; do not commit it.
- `.idea/`: IDE settings; keep editor-specific changes out of commits unless the team explicitly needs them.

Place application code under `src/` as the project is built out, and keep tests in `tests/`. Use feature-oriented module names such as `src/instruments/session.py` and mirror them in `tests/test_session.py`.

## Build, Test, and Development Commands
There is no committed build system yet, so use the local virtual environment directly.

- `.venv\Scripts\python -V`: confirm the interpreter version.
- `.venv\Scripts\python -m pip install -r requirements.txt`: install dependencies once a `requirements.txt` file exists.
- `.venv\Scripts\python -m pytest`: run the full test suite after `pytest` is added.
- `.venv\Scripts\python -m pytest tests/test_session.py`: run a focused test file during development.

If the project adds a `pyproject.toml`, prefer tool entry points from that file over ad hoc commands.

## Coding Style & Naming Conventions
Target Python 3.12, matching the configured interpreter in `.idea/misc.xml`. Use 4-space indentation, UTF-8 files, and PEP 8 naming:

- `snake_case` for modules, functions, and variables
- `PascalCase` for classes
- `UPPER_SNAKE_CASE` for constants

Prefer small modules with explicit imports. If formatting and linting are introduced, standardize on one formatter and one linter and document the exact commands here.

## Testing Guidelines
Use `pytest` for new test coverage. Name test files `test_*.py`, and keep test data close to the tests that use it. Add at least one regression test for every bug fix and cover public behavior instead of private implementation details.

## Commit & Pull Request Guidelines
This repository does not yet have commit history, so adopt a simple standard now: short imperative commit subjects such as `Add VISA session wrapper`. Keep commits focused and explain non-obvious decisions in the body.

Pull requests should include a concise summary, the commands used for validation, and any screenshots or logs when behavior changes are visible externally.

## Contributor Notes
Do not commit `.venv/`, local IDE metadata, secrets, or machine-specific configuration. Keep generated files out of version control unless they are required to build or test the project.
