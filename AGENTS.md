# Repository Guidelines

## Project Structure & Module Organization

ProgramBench is a Python 3.10+ package using the `src/` layout. Core package code lives in `src/programbench/`; Typer CLI commands live in `src/programbench/cli/`, evaluation logic in `src/programbench/eval/`, shared helpers in `src/programbench/utils/`, and packaged benchmark data in `src/programbench/data/tasks/<owner>__<repo>.<sha>/`. Tests live in `tests/`, documentation in `docs/`, and CI configuration in `.github/workflows/`.

## Build, Test, and Development Commands

- `uv sync`: install the package in editable mode with development dependencies.
- `uv run programbench --help`: run the local CLI entry point.
- `uv run pytest`: run the pytest suite configured in `pyproject.toml`.
- `uv run ruff check .` and `uv run ruff format .`: lint and format Python code.
- `uv run pre-commit run --all-files`: run the same local hooks used by CI, including Ruff and whitespace checks.
- `uv build`: build source and wheel distributions through `uv_build`.

## Coding Style & Naming Conventions

Use type-annotated Python and standard 4-space indentation; Ruff enforces a 120-character line length. Prefer `pathlib.Path` over `os.path`, built-in generics like `list[str]`, and concise code that fails clearly instead of hiding errors behind defensive fallbacks. Keep CLI and presentation concerns in `src/programbench/cli/`; core modules should remain importable without CLI display logic. Name tests as `tests/test_*.py`, functions as `test_*`, and benchmark task directories as `<owner>__<repo>.<short_sha>`.

## Testing Guidelines

Use `pytest`, not `unittest`. Add focused tests when changing parsing, scoring, batch evaluation, data loading, or CLI behavior. There is no configured coverage threshold, so prioritize tests for behavioral edge cases and failure modes. Avoid unnecessary mocking; prefer direct assertions such as `assert func() == expected`. For parametrized tests, use a tuple of argument names and a list of cases.

## Commit & Pull Request Guidelines

Recent history uses short subjects with conventional prefixes such as `fix:`, `docs:`, `build(deps):`, plus occasional capitalized forms like `Fix(dat):` or `Dev:`. Keep commits concise and imperative. Pull requests should branch from `main`, describe the change and motivation, link issues when relevant, include docs updates for API or user-facing changes, and pass `pytest` plus pre-commit checks. Meta projects require completing the Contributor License Agreement before merge.

## Agent-Specific Notes

Be conservative in `src/programbench/eval/eval.py`: evaluation errors must be surfaced in output rather than silently skipped. Do not remove the warning comment in that file, and avoid broad exception handling unless the requested behavior explicitly requires it.
