# Notes RAG MCP Server

A Model Context Protocol (MCP) server for semantic search over markdown notes and documentation. 

This server indexes your markdown notes into a Qdrant vector database using embeddings (defaulting to LiteLLM), and provides semantic search capabilities as an MCP tool, allowing LLMs to seamlessly search and retrieve context from your personal knowledge base.

## Features
- **MCP Integration**: Exposes `search_notes`, `trigger_reindex`, and `index_status` tools.
- **Smart Chunking**: Splits markdown files by headings, ensuring context is preserved in chunks.
- **Incremental Indexing**: Uses SQLite to cache file modification times and Qdrant points, only re-indexing changed files.
- **Web Dashboard**: Includes an admin dashboard at `/admin` for managing indexed paths, viewing stats, and triggering re-indexing manually.
- **Parallel Processing**: Speeds up indexing using concurrent thread pools.
- **Frontmatter Support**: Parses YAML frontmatter for tags, categories, and other metadata to enrich the vector payload.

## Tools Exposed
- `search_notes`: Perform semantic search on markdown notes and documentation. Filters by query, folder, tag, and category.
- `trigger_reindex`: Force an immediate directory scan to index new or updated files.
- `index_status`: Get indexing statistics of the vault.

## Environment Variables
- `QDRANT_URL`: URL to the Qdrant instance (default: `http://qdrant:6333`).
- `LITELLM_URL`: URL to LiteLLM instance for embeddings (default: `http://litellm:4000/v1`).
- `LITELLM_API_KEY`: API key for embeddings (default: `dummy`).
- `EMBEDDING_MODEL`: Embedding model to use (default: `text-embedding-3-small`).
- `COLLECTION_NAME`: Qdrant collection name (default: `notes_rag`).
- `VAULT_PATH`: Default path to the markdown notes vault (default: `/containers/productivity/obsidian/shared`).
- `CACHE_DB_PATH`: Path to SQLite index cache DB (default: `/app/data/index_cache.db`).
- `CHUNK_SIZE`: Max characters per chunk (default: `1500`).
- `CHUNK_OVERLAP`: Overlap between chunks (default: `200`).

## Running
Build and run the Docker container:
```sh
docker build -t notes-rag-mcp .
docker run -p 3000:3000 --env-file .env notes-rag-mcp
```
