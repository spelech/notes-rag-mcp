# Developer Documentation

This document provides instructions for developing, testing, and running the Notes RAG MCP Server locally.

## Prerequisites
- Python 3.11+
- Qdrant (can be run via Docker)
- LiteLLM or an OpenAI-compatible API endpoint

## Local Setup

1. **Clone the repository:**
   ```bash
   git clone git@github.com:spelech/notes-rag-mcp.git
   cd notes-rag-mcp
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Start Qdrant locally (if not already running):**
   ```bash
   docker run -p 6333:6333 -p 6334:6334 \
       -v $(pwd)/qdrant_storage:/qdrant/storage \
       qdrant/qdrant
   ```

5. **Set Environment Variables:**
   You can run the server with local environment overrides:
   ```bash
   export QDRANT_URL="http://localhost:6333"
   export LITELLM_URL="https://api.openai.com/v1" # Or your local LiteLLM instance
   export LITELLM_API_KEY="your-api-key"
   export VAULT_PATH="/path/to/your/markdown/notes"
   ```

6. **Run the server:**
   ```bash
   python server.py
   ```
   The FastAPI server will start, initializing Qdrant, and begin the background indexing thread.

## Admin Dashboard
- Open `http://localhost:3000/admin/` in your browser.
- You can manage paths to index and view statistics.

## Project Structure
- `server.py`: The main entrypoint containing the FastAPI application, MCP server logic, Qdrant client, and chunking/indexing engine.
- `www/`: Contains static assets (`index.html`, etc.) for the admin UI.
- `requirements.txt`: Python dependencies.
- `Dockerfile`: Container build instructions.
- `data/`: (Generated at runtime) Stores the SQLite `index_cache.db`.

## Testing the MCP endpoints
You can connect an MCP client that supports Server-Sent Events (SSE) to `http://localhost:3000/sse` to discover and use the `search_notes`, `trigger_reindex`, and `index_status` tools.
