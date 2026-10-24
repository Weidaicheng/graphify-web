/* chat.js — SSE streaming chat interface */

let chatHistory = [];
let currentGraphId = GRAPH_ID;
let isStreaming = false;

// ── Send message ───────────────────────────────────────────

async function sendMessage(e) {
    if (e) e.preventDefault();
    if (isStreaming) return;

    const input = document.getElementById('chat-input');
    const query = input.value.trim();
    if (!query) return;

    input.value = '';
    appendMessage('user', query);
    chatHistory.push({ role: 'user', content: query });

    isStreaming = true;
    const sendBtn = document.getElementById('send-btn');
    sendBtn.disabled = true;
    sendBtn.textContent = '思考中…';

    // Create assistant message placeholder
    const msgEl = appendMessage('assistant', '');
    const bodyEl = msgEl.querySelector('.chat-bubble-body');
    let fullText = '';

    try {
        const response = await fetch(`/api/chat/${currentGraphId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query: query,
                history: chatHistory.slice(-10),
            }),
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.error || `HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const dataStr = line.slice(6).trim();
                if (!dataStr) continue;

                try {
                    const event = JSON.parse(dataStr);

                    if (event.type === 'expansion') {
                        showExpansion(event);
                    } else if (event.type === 'context') {
                        showContext(event);
                    } else if (event.type === 'token') {
                        fullText += event.content;
                        // Show indicator when streaming a mermaid block
                        const openM = (fullText.match(/```mermaid\n/g) || []).length;
                        const closeM = (fullText.match(/```mermaid\n[\s\S]*?```/g) || []).length;
                        let indicator = '';
                        if (openM > closeM) {
                            indicator = '<div class="text-xs text-emerald-400 animate-pulse mt-1">⏳ 正在生成图表…</div>';
                        }
                        bodyEl.innerHTML = renderMarkdown(fullText) + indicator;
                        scrollToBottom();
                    } else if (event.type === 'done') {
                        // Stream complete — render any Mermaid diagrams
                        await renderMermaidInElement(bodyEl);
                    } else if (event.type === 'error') {
                        bodyEl.innerHTML = `<p class="text-red-400">${event.message}</p>`;
                    }
                } catch (e) {
                    // Skip malformed JSON
                }
            }
        }
    } catch (e) {
        bodyEl.innerHTML = `<p class="text-red-400">请求失败: ${e.message}</p>`;
    }

    chatHistory.push({ role: 'assistant', content: fullText });
    isStreaming = false;
    sendBtn.disabled = false;
    sendBtn.textContent = '发送';
}

// ── Append message ─────────────────────────────────────────

function appendMessage(role, text) {
    const container = document.getElementById('chat-messages');
    const el = document.createElement('div');
    el.className = `chat-bubble ${role}`;

    const avatar = role === 'user'
        ? '<span class="chat-avatar bg-blue-500/20 text-blue-400">👤</span>'
        : '<span class="chat-avatar bg-emerald-500/20 text-emerald-400">⬡</span>';
    const name = role === 'user' ? '你' : 'Graphify 助手';

    el.innerHTML = `
        <div class="chat-bubble-header">
            ${avatar}
            <span class="text-xs text-dark-200">${name}</span>
        </div>
        <div class="chat-bubble-body">
            ${text ? renderMarkdown(text) : '<span class="opacity-40">…</span>'}
        </div>
    `;

    container.appendChild(el);
    scrollToBottom();
    return el;
}

// ── Context panel ──────────────────────────────────────────

function showExpansion(event) {
    const panel = document.getElementById('expansion-panel');
    const content = document.getElementById('expansion-content');
    if (!panel) return;
    panel.classList.remove('hidden');

    const tokens = event.tokens || [];
    let html = '';
    html += `<span class="text-slate-500">原始问题:</span> <span class="text-slate-300">${event.original}</span>`;
    html += `<span class="mx-2 text-slate-600">→</span>`;
    html += `<span class="text-slate-500">模式:</span> <span class="text-cyan-400 font-mono">${event.mode || 'bfs'}</span>`;
    html += `<span class="mx-2 text-slate-600">·</span>`;
    html += `<span class="text-slate-500">词汇表:</span> <span class="text-slate-400">${event.vocab_size || 0} tokens</span>`;
    if (tokens.length > 0) {
        html += `<br><span class="text-emerald-400">扩展为:</span> `;
        html += tokens.map(t => `<span class="bg-emerald-500/15 text-emerald-300 px-1.5 py-0.5 rounded text-xs font-mono mr-1">${t}</span>`).join('');
    } else {
        html += `<br><span class="text-amber-400">未找到匹配词汇，使用原始查询</span>`;
    }
    content.innerHTML = html;
}

function showContext(event) {
    const panel = document.getElementById('context-panel');
    const content = document.getElementById('context-content');
    panel.classList.remove('hidden');

    let html = '';
    if (event.nodes && event.nodes.length > 0) {
        html += '<div class="mb-1"><span class="text-emerald-400">相关节点:</span> ';
        html += event.nodes.map(n => {
            if (n.label) return `<span class="text-white">${n.label}</span>`;
            if (n.source) return `<span class="text-white">${n.source}</span> → <span class="text-amber-400">${n.relation}</span> → <span class="text-white">${n.target}</span>`;
            return '';
        }).join(', ');
        html += '</div>';
    }
    if (event.god_nodes && event.god_nodes.length > 0) {
        html += '<div><span class="text-purple-400">核心节点:</span> ';
        html += event.god_nodes.map(n => `<span class="text-white">${n}</span>`).join(', ');
        html += '</div>';
    }
    content.innerHTML = html;
}

function toggleContext() {
    const panel = document.getElementById('context-panel');
    panel.classList.toggle('hidden');
}

// ── Suggestion chips ───────────────────────────────────────

function askSuggestion(text) {
    document.getElementById('chat-input').value = text;
    sendMessage();
}

// ── Switch graph ───────────────────────────────────────────

function switchGraph(graphId) {
    currentGraphId = graphId;
    chatHistory = [];
    // Clear messages except first
    const container = document.getElementById('chat-messages');
    container.innerHTML = '';
    // Re-add welcome
    appendMessage('assistant', '已切换到新的图谱。你可以开始提问了。');
    // Hide context
    document.getElementById('context-panel')?.classList.add('hidden');
}

// ── Markdown renderer (simple, Mermaid-aware) ──────────────

function renderMarkdown(text) {
    if (!text) return '';

    // Pass 1: Extract mermaid blocks with placeholders (preserve newlines)
    const mermaidBlocks = [];
    let processed = text.replace(/```mermaid\n([\s\S]*?)```/g, (match, diagram) => {
        const idx = mermaidBlocks.length;
        mermaidBlocks.push(diagram.trim());
        return `\n%%MERMAID_${idx}%%\n`;
    });

    // Pass 2: Standard markdown rendering
    processed = processed
        // Code blocks
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="bg-slate-900 rounded-lg p-2 my-1 overflow-x-auto text-xs"><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code class="bg-slate-800 px-1 rounded text-xs">$1</code>')
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Headers
        .replace(/^### (.+)$/gm, '<h3 class="font-bold mt-2 mb-1">$1</h3>')
        .replace(/^## (.+)$/gm, '<h2 class="font-bold mt-3 mb-1 text-base">$1</h2>')
        .replace(/^# (.+)$/gm, '<h1 class="font-bold mt-3 mb-1 text-lg">$1</h1>')
        // Lists
        .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
        .replace(/^\d+\. (.+)$/gm, '<li class="ml-4 list-decimal">$1</li>')
        // Line breaks
        .replace(/\n\n/g, '</p><p class="mt-2">')
        .replace(/\n/g, '<br>');

    // Pass 3: Restore mermaid blocks with preserved newlines
    processed = processed.replace(/%%MERMAID_(\d+)%%/g, (match, idx) => {
        const source = mermaidBlocks[parseInt(idx)];
        return `<pre class="mermaid-source bg-slate-900 rounded-lg p-3 my-2 overflow-x-auto text-xs text-emerald-300 border border-emerald-500/20 border-l-[3px]"><code>${source}</code></pre>`;
    });

    return processed;
}

// ── Mermaid rendering ──────────────────────────────────────

let mermaidCounter = 0;

function sanitizeMermaidSource(source) {
    // Auto-quote unquoted node labels that contain special chars (colons, parens, etc.)
    // Matches patterns like: A[unquoted label] → A["quoted label"]
    return source.replace(/(\w+)(\[)([^\]"]*?[():\u4e00-\u9fff][^\]]*?)(\])/g, (match, id, open, label, close) => {
        // Only quote if not already quoted
        if (label.startsWith('"') && label.endsWith('"')) return match;
        return `${id}${open}"${label}"${close}`;
    });
}

async function renderMermaidInElement(containerEl) {
    if (typeof mermaid === 'undefined') return;
    const mermaidPres = containerEl.querySelectorAll('pre.mermaid-source');
    if (mermaidPres.length === 0) return;

    for (const pre of mermaidPres) {
        const source = pre.querySelector('code')?.textContent || pre.textContent;
        const sanitized = sanitizeMermaidSource(source);
        const id = `mermaid-${Date.now()}-${++mermaidCounter}`;

        try {
            const { svg } = await mermaid.render(id, sanitized);
            const wrapper = document.createElement('div');
            wrapper.className = 'mermaid-rendered my-3 overflow-x-auto rounded-lg';
            wrapper.innerHTML = svg;
            pre.replaceWith(wrapper);
        } catch (err) {
            // If Mermaid fails to parse, leave the source visible with warning
            pre.classList.add('border-red-500/30');
            pre.classList.remove('border-emerald-500/20');
            const warning = document.createElement('div');
            warning.className = 'text-xs text-red-400 mt-1 mb-2';
            warning.textContent = '⚠ 图表渲染失败，显示源码：';
            pre.before(warning);
            // Clean up any leftover mermaid temp elements
            const tempEl = document.getElementById('d' + id);
            if (tempEl) tempEl.remove();
            console.warn('Mermaid render error:', err);
        }
    }
}

// ── Utilities ──────────────────────────────────────────────

function scrollToBottom() {
    const container = document.getElementById('chat-messages');
    container.scrollTop = container.scrollHeight;
}

// Focus input on load
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('chat-input')?.focus();
});
