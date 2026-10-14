# AgentBridge File Access & Explorer Reference

## 1. Architectural Principles & Boundaries

The Codebase Explorer distinguishes four separate size and resource boundaries:

1. **Total File Size**: The actual size of the file on disk. A large local file (50 MiB, 100 MiB, 120 MiB+) is **not** automatically unpreviewable merely because of its total size.
2. **Preview Window Size**: The slice of code requested for inspection, bounded by **both** line limits (`DEFAULT_PREVIEW_LINES = 35`, `MAX_PREVIEW_LINES = 200`) and byte limits (`MAX_PREVIEW_BYTES = 64 KB`). Whichever limit is reached first bounds the returned content.
3. **Single Operation / Scan Budget**: The maximum budget (`MAX_SINGLE_SCAN_BYTES = 50 MB`) permitted for a single search or AST scan operation. If an operation reaches this budget, it stops and reports partial results without claiming the file is too large.
4. **Export Chunk Size**: The maximum transport chunk (`MAX_DOWNLOAD_CHUNK_BYTES = 10 MB`) for single downloads. Files exceeding this size are exported via deterministic multi-part manifests up to `MAX_TOTAL_EXPORT_BYTES = 200 MB`.

> [!NOTE]
> **GitHub Limits vs. Local Preview Limits**:
> GitHub's standard Git single-file repository limit (100 MB) is completely separate from AgentBridge's local preview capability. Local files (artifacts, build outputs, Git LFS objects) can be previewed locally without querying or relying on GitHub.

---

## 2. Modules and Functions

Module: `sutra/file_access.py`

### `preview_file_bounded`
```python
def preview_file_bounded(
    target: Path,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = DEFAULT_PREVIEW_LINES,
    max_preview_bytes: int = MAX_PREVIEW_BYTES,
    expected_version: str | None = None,
) -> dict[str, Any]
```
Streams a bounded slice of source code without full memory buffering:
- Uses `SparseLineIndex` checkpoints (indexed every 1,000 lines) to seek directly to the requested line region in $O(1)$ time.
- Bounded by **both** `max_lines` (up to `MAX_PREVIEW_LINES`) and `max_preview_bytes` (default: 64 KB).
- Pathological lines (e.g. single 10 MB line) are cleanly truncated with `truncated_by_bytes = True` and clear user notification: `"⚠️ Preview truncated by size limit."`
- Redacts secrets line-by-line via `redact_secrets()`.
- Detects concurrent modifications via `version_changed`.

### `build_export_plan`
```python
def build_export_plan(
    target: Path,
    chunk_size: int = MAX_DOWNLOAD_CHUNK_BYTES,
) -> ExportManifest
```
Generates a deterministic multi-part export plan:
- Files `<= MAX_DOWNLOAD_CHUNK_BYTES` are downloaded in a single direct chunk.
- Files `> MAX_DOWNLOAD_CHUNK_BYTES` are split into deterministic parts (`<filename>.part-0001`, `<filename>.part-0002`, ...).
- Text files prefer splitting on newline boundaries while preserving UTF-8 character alignment.
- Binary files use exact byte slicing.
- Produces `ExportManifest` recording per-part SHA-256 and `whole_file_sha256`.

### `get_export_part_bytes`
```python
def get_export_part_bytes(
    target: Path,
    manifest: ExportManifest,
    part_index: int,
) -> tuple[ExportPart, bytes]
```
Retrieves the exact bytes for `part_index` (1-based) and validates its SHA-256 against the manifest. Fails with version mismatch if the file was modified on disk.

### `verify_export_integrity` & `reconstruct_file_from_parts`
```python
def verify_export_integrity(manifest: ExportManifest, parts_data: dict[int, bytes]) -> tuple[bool, str]
def reconstruct_file_from_parts(manifest: ExportManifest, parts_data: dict[int, bytes], output_path: Path) -> bool
```
Verifies part sizes, individual SHA-256 hashes, concatenated size, and reconstructed whole-file SHA-256 against the original manifest. Reconstructs the exact file byte-for-byte.

### `get_file_info`
```python
def get_file_info(target: Path, relative_to: Path | None = None) -> dict[str, Any]
```
Returns compact metadata without buffering entire files into memory:
- **`name`**: File basename
- **`size_bytes`**: Total size on disk
- **`total_lines`**: Streaming line count
- **`download_available`**: Boolean (`size <= MAX_DOWNLOAD_CHUNK_BYTES`)
- **`export_available`**: Boolean (`size <= MAX_TOTAL_EXPORT_BYTES`)
- **`is_multipart`**: Boolean indicating if multi-part export is required

### `search_file_text`
```python
def search_file_text(
    target: Path,
    query: str,
    max_results: int = 20,
    case_sensitive: bool = False,
    max_scan_bytes: int = MAX_SINGLE_SCAN_BYTES,
) -> dict[str, Any]
```
Scans lines iteratively for `query` within an operation budget (`MAX_SINGLE_SCAN_BYTES = 50 MB`). Stops scanning cleanly and reports partial matches if budget is reached.

### `extract_file_symbols`
```python
def extract_file_symbols(target: Path, max_scan_bytes: int = MAX_SINGLE_SCAN_BYTES) -> dict[str, Any]
```
Deterministic AST parsing (Python) and regex tokenizers (JS/TS/Go/Rust/Java) bounded by `MAX_SINGLE_SCAN_BYTES`.

---

## 3. Constants & Limits Reference

| Constant | Value | Purpose |
|---|---|---|
| `DEFAULT_PREVIEW_LINES` | 35 lines | Default code window height. |
| `MAX_PREVIEW_LINES` | 200 lines | Maximum lines allowed in a single preview request. |
| `MAX_PREVIEW_BYTES` | 64 KB | Maximum accumulated bytes in a preview window before size truncation. |
| `MAX_SINGLE_SCAN_BYTES` | 50 MB | Maximum budget for a single bounded scan/processing operation (Search, AST). |
| `MAX_STREAM_BYTES` | 50 MB | *(Deprecated alias)* Equal to `MAX_SINGLE_SCAN_BYTES`. |
| `MAX_DOWNLOAD_CHUNK_BYTES` | 10 MB | Maximum size of a single transport download chunk. |
| `MAX_DOWNLOAD_BYTES` | 10 MB | *(Deprecated alias)* Equal to `MAX_DOWNLOAD_CHUNK_BYTES`. |
| `MAX_TOTAL_EXPORT_BYTES` | 200 MB | Upper bound for deterministic multi-part file exports. |
| `DEFAULT_DIR_PAGE_SIZE` | 30 items | Default directory pagination size. |
| `MAX_DIR_PAGE_SIZE` | 100 items | Upper bound on directory entries per page. |
