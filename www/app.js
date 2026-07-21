document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadPaths();
    
    // Poll stats and paths every 10s
    setInterval(loadStats, 10000);
    setInterval(loadPaths, 10000);
});

// Cache global configs
window.allPaths = [];
window.currentBrowserPath = "/containers";

async function loadStats() {
    try {
        const response = await fetch('/admin/api/stats');
        if (!response.ok) throw new Error('Failed to fetch stats');
        const data = await response.json();
        
        document.getElementById('stat-paths').textContent = data.paths_count;
        document.getElementById('stat-files').textContent = data.files_count;
        document.getElementById('stat-vectors').textContent = data.points_count;
        document.getElementById('stat-last-indexed').textContent = data.last_indexed || 'Never';
        
        if (data.embedding_provider && data.embedding_model) {
            const providerEl = document.getElementById('stat-embedding-provider');
            if (providerEl) {
                providerEl.textContent = `${data.embedding_provider} (${data.embedding_model})`;
            }
        }
        
        if (data.top_keywords && Array.isArray(data.top_keywords)) {
            const cloud = document.getElementById('topics-tag-cloud');
            if (cloud) {
                if (data.top_keywords.length === 0) {
                    cloud.innerHTML = '<span style="font-size: 0.85rem; color: rgba(255,255,255,0.4);">No topics extracted yet. Trigger reindex to populate.</span>';
                } else {
                    cloud.innerHTML = data.top_keywords.map(kw => 
                        `<span style="background: rgba(20, 184, 166, 0.15); border: 1px solid rgba(20, 184, 166, 0.3); color: #2dd4bf; padding: 3px 8px; border-radius: 4px; font-size: 0.8rem; font-family: 'JetBrains Mono', monospace;">${kw}</span>`
                    ).join('');
                }
            }
        }
        
        const indicator = document.getElementById('indexing-indicator');
        if (data.is_indexing) {
            indicator.innerHTML = '<span class="indicator indexing"></span> Indexing...';
            document.getElementById('btn-reindex').disabled = true;
            document.getElementById('btn-reindex').innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Indexing...';
        } else {
            indicator.innerHTML = '<span class="indicator online"></span> Idle';
            document.getElementById('btn-reindex').disabled = false;
            document.getElementById('btn-reindex').innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Reindex Source Files';
        }

    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

async function loadPaths() {
    try {
        const response = await fetch('/admin/api/paths');
        if (!response.ok) throw new Error('Failed to fetch paths');
        const paths = await response.json();
        window.allPaths = paths;
        
        renderPaths(paths);
    } catch (error) {
        console.error('Error loading paths:', error);
    }
}

function renderPaths(paths) {
    const tbody = document.getElementById('paths-list-body');
    if (paths.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="empty-state">No source paths configured. Defaulting to vault directory scan.</td>
            </tr>
        `;
        return;
    }
    
    tbody.innerHTML = paths.map(path => {
        const pathClass = path.enabled ? 'code' : 'code text-muted';
        const typeBadge = path.type === 'directory' 
            ? `<span class="server-badge"><i class="fa-solid fa-folder"></i> Directory</span>`
            : `<span class="server-badge"><i class="fa-solid fa-file"></i> File</span>`;
            
        const categoryVal = path.category && path.category !== 'default' 
            ? `<span class="server-badge" style="background: rgba(20,184,166,0.15); color: var(--accent);">${escapeHtml(path.category)}</span>`
            : '<span class="text-muted" style="font-size:11px;">Default</span>';
            
        const recursiveVal = path.type === 'directory'
            ? (path.recursive ? 'Yes' : 'No (Top-level)')
            : '<span class="text-muted">-</span>';
            
        return `
            <tr>
                <td><code class="${pathClass}">${escapeHtml(path.path)}</code></td>
                <td>${typeBadge}</td>
                <td>${recursiveVal}</td>
                <td>${categoryVal}</td>
                <td>
                    <label class="switch">
                        <input type="checkbox" ${path.enabled ? 'checked' : ''} onchange="togglePathEnabled('${path.id}', this.checked)">
                        <span class="slider"></span>
                    </label>
                </td>
                <td>
                    <div class="server-actions" style="gap: 5px; justify-content: flex-start;">
                        <button class="btn-icon btn-edit" title="Edit Settings" onclick="openEditModal('${path.id}')">
                            <i class="fa-solid fa-pen-to-square"></i>
                        </button>
                        <button class="btn-icon btn-delete" title="Delete Path" onclick="deletePath('${path.id}', '${escapeHtml(path.path)}')">
                            <i class="fa-solid fa-trash-can"></i>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

async function triggerReindex() {
    try {
        const btn = document.getElementById('btn-reindex');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Triggering...';
        
        const response = await fetch('/admin/api/reindex', { method: 'POST' });
        if (!response.ok) throw new Error('Failed to trigger reindexing');
        
        loadStats();
    } catch (error) {
        alert(`Error triggering reindex: ${error.message}`);
        loadStats();
    }
}

async function togglePathEnabled(id, enabled) {
    try {
        const path = window.allPaths.find(p => p.id == id);
        if (!path) return;
        
        const body = {
            enabled: enabled ? 1 : 0,
            recursive: path.recursive,
            category: path.category
        };
        
        const response = await fetch(`/admin/api/paths/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
        if (!response.ok) throw new Error('Failed to update path settings');
        loadPaths();
        loadStats();
    } catch (error) {
        alert(`Error: ${error.message}`);
        loadPaths();
    }
}

function openAddModal() {
    document.getElementById('modal-title').innerHTML = '<i class="fa-solid fa-folder-open"></i> Add RAG Search Path';
    document.getElementById('path-id').value = '';
    document.getElementById('path-form').reset();
    document.getElementById('path-enabled').checked = true;
    toggleRecursiveField();
    document.getElementById('path-modal').style.display = 'flex';
}

function openEditModal(id) {
    const path = window.allPaths.find(p => p.id == id);
    if (!path) return;
    
    document.getElementById('modal-title').innerHTML = '<i class="fa-solid fa-pen-to-square"></i> Edit RAG Search Path';
    document.getElementById('path-id').value = path.id;
    document.getElementById('selected-path').value = path.path;
    document.getElementById('path-type').value = path.type;
    document.getElementById('path-recursive').value = path.recursive ? "1" : "0";
    document.getElementById('path-category').value = path.category || '';
    document.getElementById('path-enabled').checked = path.enabled ? true : false;
    
    toggleRecursiveField();
    document.getElementById('path-modal').style.display = 'flex';
}

function closeModal() {
    document.getElementById('path-modal').style.display = 'none';
}

function toggleRecursiveField() {
    const type = document.getElementById('path-type').value;
    const group = document.getElementById('recursive-group');
    if (type === 'file') {
        group.style.display = 'none';
    } else {
        group.style.display = 'flex';
    }
}

async function savePath(event) {
    event.preventDefault();
    const id = document.getElementById('path-id').value;
    const pathVal = document.getElementById('selected-path').value;
    const typeVal = document.getElementById('path-type').value;
    const recVal = typeVal === 'directory' ? parseInt(document.getElementById('path-recursive').value) : 0;
    const catVal = document.getElementById('path-category').value.trim() || null;
    const enabledVal = document.getElementById('path-enabled').checked ? 1 : 0;
    
    const body = {
        path: pathVal,
        type: typeVal,
        recursive: recVal,
        category: catVal,
        enabled: enabledVal
    };
    
    try {
        let response;
        if (id) {
            response = await fetch(`/admin/api/paths/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
        } else {
            response = await fetch('/admin/api/paths', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
        }
        
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.error || 'Failed to save search path');
        }
        
        closeModal();
        loadPaths();
        loadStats();
    } catch (error) {
        alert(`Error: ${error.message}`);
    }
}

async function deletePath(id, path) {
    if (!confirm(`Are you sure you want to delete the path '${path}'?\nThis will remove all associated files and vectors from the RAG index.`)) return;
    try {
        const response = await fetch(`/admin/api/paths/${id}`, { method: 'DELETE' });
        if (!response.ok) throw new Error('Failed to delete path');
        loadPaths();
        loadStats();
    } catch (error) {
        alert(`Error deleting path: ${error.message}`);
    }
}

// ----------------------------------------------------
// DIRECTORY BROWSER STATE MACHINE
// ----------------------------------------------------

function openBrowser() {
    const existingPath = document.getElementById('selected-path').value;
    // Start at currently configured path, or default /containers
    let startPath = "/containers";
    if (existingPath && existingPath.startsWith("/")) {
        startPath = existingPath;
    }
    
    document.getElementById('browser-modal').style.display = 'flex';
    browseTo(startPath);
}

function closeBrowser() {
    document.getElementById('browser-modal').style.display = 'none';
}

async function browseTo(path) {
    try {
        const list = document.getElementById('browser-list');
        list.innerHTML = '<div class="loading-state" style="padding: 20px;"><i class="fa-solid fa-spinner fa-spin"></i> Loading...</div>';
        
        const response = await fetch(`/admin/api/browse?path=${encodeURIComponent(path)}`);
        if (!response.ok) throw new Error('Failed to read directory');
        const data = await response.json();
        
        window.currentBrowserPath = data.current_path;
        document.getElementById('current-browser-path').textContent = data.current_path;
        
        renderBrowserList(data);
    } catch (error) {
        alert(`Failed to browse path: ${error.message}`);
    }
}

function renderBrowserList(data) {
    const list = document.getElementById('browser-list');
    let items = [];
    
    // Add Go Up item if parent path exists
    if (data.parent_path) {
        items.push(`
            <li class="browser-item parent-link" onclick="browseTo('${escapeHtml(data.parent_path)}')">
                <i class="fa-solid fa-arrow-turn-up icon-dir"></i>
                <span class="name">.. (Go Up)</span>
            </li>
        `);
    }
    
    // Add directories
    data.directories.forEach(dir => {
        items.push(`
            <li class="browser-item dir-item">
                <div class="item-click-target" onclick="browseTo('${escapeHtml(dir.path)}')">
                    <i class="fa-solid fa-folder icon-dir"></i>
                    <span class="name">${escapeHtml(dir.name)}</span>
                </div>
                <button type="button" class="btn btn-secondary btn-select-inline" onclick="selectInlinePath('${escapeHtml(dir.path)}', 'directory')">
                    Select
                </button>
            </li>
        `);
    });
    
    // Add files
    data.files.forEach(file => {
        items.push(`
            <li class="browser-item file-item">
                <div class="item-click-target">
                    <i class="fa-solid fa-file-lines icon-file"></i>
                    <span class="name">${escapeHtml(file.name)}</span>
                </div>
                <button type="button" class="btn btn-secondary btn-select-inline" onclick="selectInlinePath('${escapeHtml(file.path)}', 'file')">
                    Select
                </button>
            </li>
        `);
    });
    
    if (items.length === 0) {
        list.innerHTML = '<div class="empty-state" style="padding: 20px;">Directory is empty.</div>';
    } else {
        list.innerHTML = items.join('');
    }
}

function selectInlinePath(path, type) {
    document.getElementById('selected-path').value = path;
    document.getElementById('path-type').value = type;
    toggleRecursiveField();
    closeBrowser();
}

function selectBrowserPath() {
    document.getElementById('selected-path').value = window.currentBrowserPath;
    document.getElementById('path-type').value = 'directory';
    toggleRecursiveField();
    closeBrowser();
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;")
              .replace(/"/g, "&quot;")
              .replace(/'/g, "&#039;");
}
