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
                        bodyEl.innerHTML = renderMarkdown(fullText);
                        scrollToBottom();
                    } else if (event.type === 'done') {
                        // Stream complete
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

// ── Markdown renderer (simple) ─────────────────────────────

function renderMarkdown(text) {
    if (!text) return '';
    return text
        // Code blocks
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="bg-dark-900 rounded p-2 my-1 overflow-x-auto text-xs"><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code class="bg-dark-900 px-1 rounded text-xs">$1</code>')
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
