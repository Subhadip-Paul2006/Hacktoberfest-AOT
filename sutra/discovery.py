"""Bounded, deterministic project discovery within explicitly approved roots."""
from __future__ import annotations

from pathlib import Path

from .git import GitManager
from .security import is_path_allowed, validate_access_root

PROJECT_MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "gemfile", "dockerfile", "cmakelists.txt", "makefile",
}
PROJECT_LAYOUTS = {"src", "app", "tests", "test", "backend", "frontend"}
IGNORED_DIRECTORY_NAMES = {
    ".sutra", ".codex", ".git", ".hg", ".svn", ".venv", "venv", "env", ".tox",
    ".cache", ".mypy_cache", ".pytest_cache", ".next", ".turbo", "node_modules",
    "__pycache__", "build", "dist", "out", "target", "vendor", "data",
    "models", "checkpoints", "notebooks", "screenshots", "media", "photos", "videos",
    "movies", "games", "vm", "vms", "virtual machines", "cache", "caches", "temp",
    "tmp",
}
MAX_DISCOVERY_DEPTH = 4
MAX_DISCOVERY_DIRECTORIES = 1000
MIN_CANDIDATE_SCORE = 2
PROJECT_SCORE = 5
MAX_CANDIDATE_PROJECTS_PER_ROOT = 100
MAX_PROJECTS_PER_ROOT = 100


def _ignored(path: Path) -> bool:
    name = path.name.casefold().strip()
    return name in IGNORED_DIRECTORY_NAMES or name.startswith("test_dataset")


def _project_score(path: Path) -> int:
    score = 0
    try:
        entries = list(path.iterdir())
    except (OSError, PermissionError):
        return 0
    names = {entry.name.casefold(): entry for entry in entries}
    if ".git" in names:
        score += 6
    score += min(10, 5 * sum(1 for name in PROJECT_MANIFESTS if name in names))
    if "readme.md" in names or "readme" in names:
        score += 1
    score += min(4, 2 * sum(1 for name in PROJECT_LAYOUTS if name in names
                            and names[name].is_dir() and not names[name].is_symlink()))
    source_files = sum(1 for entry in entries[:200] if entry.is_file() and entry.suffix.casefold() in {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs", ".cpp", ".c",
    })
    if source_files >= 2:
        score += 2
    if any(path.glob("*.sln")):
        score += 5
    return score


def discover_projects(registered_workspaces: list[str | Path]) -> list[dict[str, object]]:
    """Find likely projects with a depth and directory-count bound per access root."""
    projects: list[dict[str, object]] = []
    seen_projects: set[str] = set()
    for raw_root in registered_workspaces:
        try:
            root = validate_access_root(raw_root)
        except (OSError, RuntimeError, ValueError):
            continue
        queue: list[tuple[Path, int]] = [(root, 0)]
        visited = 0
        root_candidate_count = 0
        while queue and visited < MAX_DISCOVERY_DIRECTORIES:
            path, depth = queue.pop(0)
            if path != root and _ignored(path):
                continue
            if not is_path_allowed(path, [root]):
                continue
            visited += 1
            score = _project_score(path)
            is_project = score >= MIN_CANDIDATE_SCORE and path != Path(path.anchor)
            if is_project:
                canonical = str(path.resolve()).casefold()
                if canonical not in seen_projects:
                    is_candidate = score < PROJECT_SCORE
                    if not is_candidate or root_candidate_count < MAX_CANDIDATE_PROJECTS_PER_ROOT:
                        if is_candidate:
                            root_candidate_count += 1
                        seen_projects.add(canonical)
                        projects.append({
                            "name": path.name,
                            "absolute_path": str(path),
                            "relative_path": "." if path == root else path.relative_to(root).as_posix(),
                            "is_git_repository": GitManager.is_repository(path),
                            "has_package_json": (path / "package.json").is_file(),
                            "has_requirements": (path / "requirements.txt").is_file(),
                            "has_pyproject": (path / "pyproject.toml").is_file(),
                            "has_docker": any((path / name).exists() for name in
                                ("Dockerfile", "docker-compose.yml", "compose.yaml", "compose.yml")),
                            "discovery": "project" if score >= PROJECT_SCORE else "candidate",
                            "discovery_score": score,
                        })
                if score >= PROJECT_SCORE:
                    continue
            if depth >= MAX_DISCOVERY_DEPTH:
                continue
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name.casefold())
            except (OSError, PermissionError):
                continue
            for child in children:
                if visited + len(queue) >= MAX_DISCOVERY_DIRECTORIES:
                    break
                if child.is_dir() and not child.is_symlink() and not _ignored(child):
                    queue.append((child, depth + 1))
    return sorted(projects, key=lambda item: str(item["absolute_path"]).casefold())
