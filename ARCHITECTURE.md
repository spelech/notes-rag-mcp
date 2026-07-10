# Architecture

The Notes RAG MCP Server consists of several interoperating components designed to provide fast and accurate semantic search over markdown files.

## Components

1. **FastAPI Web Server**
   - Serves the `/admin` UI (HTML/JS) from the `www` directory.
   - Provides REST APIs (`/admin/api/*`) to manage indexed paths, fetch statistics, and manually trigger indexing.
   - Hosts the Model Context Protocol (MCP) endpoints via Server-Sent Events (SSE) at `/sse` and POST at `/messages`.

2. **Model Context Protocol (MCP) Server**
   - Implemented using the `mcp.server.Server` class.
   - Defines and exposes the tools (`search_notes`, `trigger_reindex`, `index_status`) that LLM clients can discover and call.
   - Translates LLM search queries into semantic vectors and queries Qdrant for related context.

3. **Indexing Engine**
   - **Cache Database (SQLite)**: Tracks the modification times of indexed files, configured paths, and basic metadata. Prevents redundant embeddings and network calls on subsequent indexing runs.
   - **Embedding Client (LiteLLM / OpenAI SDK)**: Generates vector embeddings for document chunks using models like `text-embedding-3-small`.
   - **Vector Database (Qdrant)**: Stores chunk payloads and their corresponding vectors for fast cosine-similarity search.
   - **Chunking Logic**: Reads markdown, splits content contextually by headers (`#`), and limits chunk sizes while maintaining an overlap for context retention.
   - **Concurrency**: Employs `concurrent.futures.ThreadPoolExecutor` for parallel file reading, chunking, and embedding generation to optimize performance on large note vaults.

## Workflow

1. **Initialization**: The server starts, creates default SQLite tables, and spawns a background thread to begin the incremental indexing of the default `VAULT_PATH` or custom database paths.
2. **Indexing**: 
   - Files are crawled and checked against the cache DB.
   - Changed or new files are parsed, chunked, embedded, and bulk-upserted to Qdrant.
   - Missing or deleted files are removed from Qdrant and SQLite.
3. **Serving Queries**:
   - An LLM calls `search_notes` with a query string.
   - The query is embedded using LiteLLM.
   - Qdrant is queried with the vector, returning the top matching chunks based on cosine similarity, optionally applying metadata filters (tags/categories).
   - Chunks are formatted and returned to the LLM as text content.
