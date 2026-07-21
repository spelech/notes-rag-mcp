# Architecture

The Notes RAG MCP Server consists of several interoperating components designed to provide fast, local, and accurate semantic search over markdown notes, documentation, and codebase files.

## Components

1. **FastAPI Web Server**
   - Serves the `/admin` UI (HTML/JS) from the `www` directory.
   - Exposes REST APIs (`/admin/api/*`) to manage source paths, fetch statistics, retrieve extracted top keywords, and manually trigger reindexing.
   - Hosts Model Context Protocol (MCP) endpoints via Server-Sent Events (SSE) at `/sse` and POST message relay at `/messages`.

2. **Model Context Protocol (MCP) Server**
   - Implemented using the `mcp.server.Server` class (version `1.1.0`).
   - Dynamically generates tool descriptions in `list_tools()` listing active indexed document titles, categories, and key concept topics.
   - Exposes tools (`search_notes`, `trigger_reindex`, `index_status`), resources (`notes://catalog/summary`), and prompts (`search_infrastructure_docs`).

3. **Indexing Engine**
   - **Cache & Topic Database (SQLite)**: Tracks modification times (`indexed_files`), custom source paths (`indexed_paths`), system metadata (`system_metadata`), and document summaries/extracted topics (`file_summaries`).
   - **Embedding Engine (FastEmbed / LiteLLM Fallback)**: Generates vector embeddings locally in-process using CPU-optimized ONNX models (`BAAI/bge-small-en-v1.5`, 384 dimensions). Fallback support for external OpenAI/LiteLLM endpoints.
   - **Vector Database (Qdrant)**: Stores 384-dimensional vector points and JSON payloads for cosine-similarity retrieval. Automatically handles collection recreation on vector dimension changes.
   - **Contextual Chunking**: Splits markdown contextually by headings (`#`), prepending document titles, section headers, and tags to chunk text before vector embedding.
   - **Thread-Safe Concurrency**: Uses `concurrent.futures.ThreadPoolExecutor` for parallel file processing and a thread-safe `fastembed_lock` around in-process ONNX inference.

## Workflow

1. **Initialization**: The server starts up, initializes SQLite tables, verifies Qdrant collection dimensions against the active embedding model, and spawns a background thread for directory scanning.
2. **Indexing**: 
   - Files are crawled and checked against SQLite modification times (`mtime`).
   - Changed or new files are parsed for frontmatter, headings, and key concepts, chunked with contextual breadcrumbs, embedded via FastEmbed, and bulk-upserted to Qdrant.
   - Metadata and extracted keywords are cached in `file_summaries`.
   - Deleted files are pruned from Qdrant and SQLite.
3. **Serving Queries**:
   - Client LLM calls `search_notes` with a query string.
   - Query is embedded locally in ~5ms.
   - Qdrant returns top matching chunks based on cosine similarity.
   - Formatted text content is returned to the client LLM.
