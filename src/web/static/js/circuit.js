/* Circuit tab — chat interface for the LLM circuit agent (SSE streaming) */

import { API, state } from './state.js';
import { setData as setViewportData } from './viewport.js';
import { enablePlacementTab, resetPlacementPanel } from './placement.js';
import { resetRoutingPanel } from './routing.js';
import { getSelectedModel } from './theme.js';

const messagesDiv = () => document.getElementById('circuit-chat-messages');
const statusSpan  = () => document.getElementById('circuit-status');
let _isSending = false;

// ── Load conversation history ─────────────────────────────────────

export async function loadCircuitConversation() {
    const container = messagesDiv();
    if (!container) return;
    container.innerHTML = '';
    if (!state.session) return;

    try {
        const res = await fetch(
            `${API}/api/session/circuit/conversation?session=${encodeURIComponent(state.session)}`
        );
        if (res.ok) {
            const messages = await res.json();
            if (Array.isArray(messages) && messages.length > 0) {
                renderConversation(messages);
            }
        }
    } catch { /* empty chat is fine */ }

    // Always try to show circuit result even if there is no conversation
    loadCircuitResult();
}

function renderConversation(messages) {
    for (const msg of messages) {
        if (msg.role === 'user') {
            if (typeof msg.content === 'string') {
                appendMessage('user', msg.content);
            }
        } else if (msg.role === 'assistant') {
            renderAssistantBlocks(msg.content);
        }
    }
}

function renderAssistantBlocks(blocks) {
    if (!Array.isArray(blocks)) return;
    let toolItems = [];

    const flushToolItems = () => {
        if (toolItems.length === 0) return;
        const div = document.createElement('div');
        div.className = 'chat-bubble tool-group';
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.className = 'tool-group-header';
        summary.innerHTML = `<span class="tool-icon">🔧</span> ${toolItems.length} tool call${toolItems.length > 1 ? 's' : ''}`;
        const items = document.createElement('div');
        items.className = 'tool-group-items';
        for (const el of toolItems) items.appendChild(el);
        details.appendChild(summary);
        details.appendChild(items);
        div.appendChild(details);
        messagesDiv().appendChild(div);
        toolItems = [];
    };

    for (const block of blocks) {
        switch (block.type) {
            case 'thinking':
                flushToolItems();
                if (block.thinking) {
                    const pre = createThinkingBubble(false);
                    pre.textContent = block.thinking;
                }
                break;
            case 'text':
                flushToolItems();
                if (block.text) {
                    const div = createMessageBubble();
                    div.innerHTML = renderMarkdown(block.text);
                }
                break;
            case 'tool_use':
                toolItems.push(appendToolCallStatic(block.name, block.input));
                break;
        }
    }
    flushToolItems();
}

async function loadCircuitResult() {
    if (!state.session) return;
    try {
        const res = await fetch(
            `${API}/api/session/circuit/result?session=${encodeURIComponent(state.session)}`
        );
        if (!res.ok) return;
        const design = await res.json();
        if (design && design.components) {
            appendCircuitResult(design);
            setViewportData('circuit', design);
        }
    } catch { /* no circuit yet */ }
}

// ── Enable / disable ──────────────────────────────────────────────

export function enableCircuitTab(flash = false) {
    const btn = document.querySelector('#pipeline-nav .step[data-step="circuit"]');
    if (!btn) return;
    btn.disabled = false;
    btn.classList.toggle('tab-flash', flash);
}

export function resetCircuitPanel() {
    const container = messagesDiv();
    if (container) container.innerHTML = '';
    const s = statusSpan();
    if (s) s.textContent = '';
}

// ── Send prompt ───────────────────────────────────────────────────

export async function sendCircuitPrompt() {
    const input = document.getElementById('circuit-chat-input');
    const prompt = input.value.trim();
    if (_isSending) return;
    // Allow empty prompt — server auto-generates from design context
    _isSending = true;

    if (prompt) appendMessage('user', prompt);
    input.value = '';
    input.disabled = true;
    document.getElementById('btn-send-circuit').disabled = true;
    statusSpan().textContent = 'Connecting…';

    try {
        const url = `${API}/api/session/circuit?session=${encodeURIComponent(state.session)}`;
        const postRes = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, model: getSelectedModel() }),
        });

        if (!postRes.ok) {
            const err = await postRes.text();
            appendMessage('error', `Server error: ${err}`);
            return;
        }

        // Open SSE stream to consume events
        const streamUrl = `${API}/api/session/circuit/stream?session=${encodeURIComponent(state.session)}`;
        const sseRes = await fetch(streamUrl);
        if (!sseRes.ok) {
            appendMessage('error', `Failed to open event stream`);
            return;
        }
        await consumeSSE(sseRes);
    } catch (e) {
        appendMessage('error', `Connection error: ${e.message}`);
    } finally {
        _isSending = false;
        input.disabled = false;
        document.getElementById('btn-send-circuit').disabled = false;
        statusSpan().textContent = '';
    }
}

// ── SSE parser ────────────────────────────────────────────────────

async function consumeSSE(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    let thinkingPre = null;
    let messageBubble = null;
    let messageBubbleText = '';
    let currentBlock = null;
    let toolGroup = null;
    let toolGroupItems = null;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();

        for (const part of parts) {
            if (!part.trim()) continue;

            let eventType = 'message';
            let dataStr = '';

            for (const line of part.split('\n')) {
                if (line.startsWith('event: ')) {
                    eventType = line.slice(7).trim();
                } else if (line.startsWith('data: ')) {
                    dataStr += line.slice(6);
                } else if (line.startsWith('data:')) {
                    dataStr += line.slice(5);
                }
            }

            let data = {};
            if (dataStr) {
                try { data = JSON.parse(dataStr); } catch { data = {}; }
            }

            switch (eventType) {
                case 'thinking_start':
                    currentBlock = 'thinking';
                    toolGroup = null;
                    toolGroupItems = null;
                    thinkingPre = createThinkingBubble();
                    statusSpan().textContent = 'Thinking…';
                    break;

                case 'thinking_delta':
                    if (thinkingPre && data.text) {
                        thinkingPre.textContent += data.text;
                        scrollToBottom();
                    }
                    break;

                case 'message_start':
                    currentBlock = 'message';
                    toolGroup = null;
                    toolGroupItems = null;
                    messageBubble = createMessageBubble();
                    messageBubbleText = '';
                    statusSpan().textContent = '';
                    break;

                case 'message_delta':
                    if (messageBubble && data.text) {
                        messageBubbleText += data.text;
                        messageBubble.innerHTML = renderMarkdown(messageBubbleText);
                        scrollToBottom();
                    }
                    break;

                case 'block_stop':
                    if (currentBlock === 'thinking') thinkingPre = null;
                    else if (currentBlock === 'message') {
                        messageBubble = null;
                        messageBubbleText = '';
                    }
                    currentBlock = null;
                    break;

                case 'tool_call': {
                    if (!toolGroup) {
                        const g = createToolGroup();
                        toolGroup = g.details;
                        toolGroupItems = g.items;
                    }
                    appendToolItem(toolGroupItems, data.name, data.input);
                    statusSpan().textContent = `Calling ${data.name}…`;
                    break;
                }

                case 'tool_result':
                    if (toolGroupItems) {
                        appendToolItemResult(toolGroupItems, data.name, data.content);
                    }
                    statusSpan().textContent = 'Thinking…';
                    break;

                case 'design':
                    appendCircuitResult(data.design);
                    setViewportData('circuit', data.design);
                    statusSpan().textContent = 'Circuit complete!';
                    // Enable placement step, invalidate downstream
                    enablePlacementTab(true);
                    resetPlacementPanel();
                    resetRoutingPanel();
                    {
                        const rBtn = document.querySelector('#pipeline-nav .step[data-step="routing"]');
                        if (rBtn) { rBtn.disabled = true; rBtn.classList.remove('tab-flash'); }
                    }
                    break;

                case 'error':
                    appendMessage('error', data.message || 'Unknown error');
                    statusSpan().textContent = 'Error';
                    break;

                case 'done':
                    break;
            }
        }
    }
}

// ── Render helpers ────────────────────────────────────────────────

function appendMessage(role, text) {
    const div = document.createElement('div');
    div.className = `chat-bubble ${role}`;
    div.textContent = text;
    messagesDiv().appendChild(div);
    scrollToBottom();
}

function createThinkingBubble(open = true) {
    const div = document.createElement('div');
    div.className = 'chat-bubble thinking';
    const details = document.createElement('details');
    details.open = open;
    const summary = document.createElement('summary');
    summary.textContent = '💭 Thinking…';
    const pre = document.createElement('pre');
    pre.className = 'thinking-text';
    details.appendChild(summary);
    details.appendChild(pre);
    div.appendChild(details);
    messagesDiv().appendChild(div);
    scrollToBottom();
    return pre;
}

function createMessageBubble() {
    const div = document.createElement('div');
    div.className = 'chat-bubble assistant';
    messagesDiv().appendChild(div);
    scrollToBottom();
    return div;
}

function createToolGroup() {
    const div = document.createElement('div');
    div.className = 'chat-bubble tool-group';
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.className = 'tool-group-header';
    summary.innerHTML = '<span class="tool-icon">🔧</span> Tool calls';
    const items = document.createElement('div');
    items.className = 'tool-group-items';
    details.appendChild(summary);
    details.appendChild(items);
    div.appendChild(details);
    messagesDiv().appendChild(div);
    scrollToBottom();
    return { details, items };
}

function appendToolItem(container, name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    item.dataset.toolName = name;
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">${escapeHtml(name)}</span>${escapeHtml(inputStr)}`;
    container.appendChild(item);
    const summary = container.parentElement.querySelector('.tool-group-header');
    const count = container.children.length;
    summary.innerHTML = `<span class="tool-icon">🔧</span> ${count} tool call${count > 1 ? 's' : ''}`;
    scrollToBottom();
}

function appendToolItemResult(container, name) {
    const items = container.querySelectorAll(`.tool-item[data-tool-name="${name}"]`);
    const item = items[items.length - 1];
    if (!item) return;
    const nameSpan = item.querySelector('.tool-name');
    if (nameSpan) nameSpan.textContent = `✓ ${name}`;
}

function appendToolCallStatic(name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">✓ ${escapeHtml(name)}</span>${escapeHtml(inputStr)}`;
    return item;
}

function appendCircuitResult(design) {
    const div = document.createElement('div');
    div.className = 'chat-bubble design-result';
    const compCount = design.components?.length || 0;
    const netCount = design.nets?.length || 0;
    div.innerHTML = `
        <div class="design-summary">
            <strong>✅ Circuit Complete</strong>
            <span>${compCount} components · ${netCount} nets</span>
        </div>
        <details>
            <summary>View circuit JSON</summary>
            <pre class="design-json">${escapeHtml(JSON.stringify(design, null, 2))}</pre>
        </details>
    `;
    messagesDiv().appendChild(div);
    scrollToBottom();
}

function scrollToBottom() {
    const container = messagesDiv();
    if (!container) return;
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
    if (atBottom) container.scrollTop = container.scrollHeight;
}

// ── Markdown renderer (same lightweight approach as design.js) ────

function renderMarkdown(text) {
    const lines = text.split('\n');
    const out = [];
    let inUl = false, inOl = false, inCode = false;
    let codeLines = [];

    const closeUl = () => { if (inUl) { out.push('</ul>'); inUl = false; } };
    const closeOl = () => { if (inOl) { out.push('</ol>'); inOl = false; } };
    const closeLists = () => { closeUl(); closeOl(); };

    for (const line of lines) {
        if (/^```/.test(line)) {
            if (inCode) {
                out.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
                inCode = false; codeLines = [];
            } else { closeLists(); inCode = true; codeLines = []; }
            continue;
        }
        if (inCode) { codeLines.push(line); continue; }
        const hm = line.match(/^(#{1,4})\s+(.+)/);
        if (hm) { closeLists(); const tag = `h${Math.min(hm[1].length + 3, 6)}`; out.push(`<${tag} class="md-heading">${inlineMarkdown(hm[2])}</${tag}>`); continue; }
        if (/^[-*] /.test(line)) { closeOl(); if (!inUl) { out.push('<ul>'); inUl = true; } out.push(`<li>${inlineMarkdown(line.slice(2))}</li>`); continue; }
        const olm = line.match(/^(\d+)[.)]\s+(.+)/);
        if (olm) { closeUl(); if (!inOl) { out.push('<ol>'); inOl = true; } out.push(`<li>${inlineMarkdown(olm[2])}</li>`); continue; }
        closeLists();
        if (line.trim() === '') out.push('<br>'); else out.push(`<p>${inlineMarkdown(line)}</p>`);
    }
    if (inCode && codeLines.length) out.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    closeLists();
    return out.join('');
}

function inlineMarkdown(raw) {
    return raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`(.+?)`/g, '<code>$1</code>');
}

function escapeHtml(text) {
    const el = document.createElement('div');
    el.textContent = text;
    return el.innerHTML;
}
