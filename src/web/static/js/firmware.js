/* Firmware generation panel — deterministic scaffold + AI agent + chat corrections */

import { API, state } from './state.js';

const MAX_PREVIEW_LINES = 50;
let _agentSending = false;

// ── Init ──────────────────────────────────────────────────────────

export function initFirmwarePanel() {
    const section = document.getElementById('firmware-section');
    if (!section) return;

    // "Generate Sketch" → triggers AI agent with a default prompt
    document.getElementById('btn-gen-firmware')?.addEventListener('click', () => sendFirmwarePrompt(''));
    document.getElementById('btn-download-firmware')?.addEventListener('click', downloadFirmware);
    document.getElementById('btn-open-simulator')?.addEventListener('click', () => {
        import('./simulator.js').then(m => m.openSimulator());
    });

    // Chat send button + Enter key
    document.getElementById('btn-send-firmware')?.addEventListener('click', () => {
        const input = document.getElementById('firmware-chat-input');
        sendFirmwarePrompt(input?.value?.trim() ?? '');
    });
    document.getElementById('firmware-chat-input')?.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const input = document.getElementById('firmware-chat-input');
            sendFirmwarePrompt(input?.value?.trim() ?? '');
        }
    });
}

export function showFirmwareSection() {
    const section = document.getElementById('firmware-section');
    if (section) section.hidden = false;
}

// ── Load existing state on session open ──────────────────────────

export async function loadFirmwareResult() {
    if (!state.session) return;
    // Load saved conversation history
    try {
        const res = await fetch(
            `${API}/api/session/firmware/conversation?session=${encodeURIComponent(state.session)}`,
        );
        if (res.ok) {
            const messages = await res.json();
            if (Array.isArray(messages) && messages.length > 0) {
                showFirmwareSection();
                _renderConversation(messages);
            }
        }
    } catch { /* ok */ }

    // Load saved sketch for the preview
    try {
        const res = await fetch(
            `${API}/api/session/firmware/result?session=${encodeURIComponent(state.session)}`,
        );
        if (!res.ok) return;
        const data = await res.json();
        if (data.sketch) {
            showFirmwareSection();
            _renderSketchPreview(data.sketch);
            document.getElementById('btn-download-firmware').hidden = false;
        }
    } catch { /* no firmware yet */ }
}

// ── Send prompt to agent ──────────────────────────────────────────

export async function sendFirmwarePrompt(prompt) {
    if (!state.session || _agentSending) return;
    _agentSending = true;

    const input    = document.getElementById('firmware-chat-input');
    const btnSend  = document.getElementById('btn-send-firmware');
    const btnGen   = document.getElementById('btn-gen-firmware');
    const agentSt  = document.getElementById('firmware-agent-status');

    if (input) { input.disabled = true; input.value = ''; }
    if (btnSend) btnSend.disabled = true;
    if (btnGen)  btnGen.disabled  = true;
    if (agentSt) agentSt.textContent = 'Connecting…';

    if (prompt) _appendMessage('user', prompt);
    showFirmwareSection();

    try {
        const postRes = await fetch(
            `${API}/api/session/firmware/agent?session=${encodeURIComponent(state.session)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt }),
            },
        );
        if (!postRes.ok) {
            const err = await postRes.json().catch(() => ({ detail: postRes.statusText }));
            _appendMessage('error', err.detail || 'Failed to start firmware agent');
            return;
        }

        // Open SSE stream — with reconnect on unexpected closure
        const streamBase = `${API}/api/session/firmware/agent/stream?session=${encodeURIComponent(state.session)}`;
        let cursor = 0;
        const MAX_RECONNECTS = 5;
        for (let attempt = 0; attempt <= MAX_RECONNECTS; attempt++) {
            const url = cursor > 0 ? `${streamBase}&after=${cursor}` : streamBase;
            let sseRes;
            try {
                sseRes = await fetch(url);
            } catch (fetchErr) {
                _appendMessage('error', `Connection error: ${fetchErr.message}`);
                break;
            }
            if (!sseRes.ok) {
                _appendMessage('error', `Failed to open event stream (${sseRes.status})`);
                break;
            }
            const result = await _consumeSSE(sseRes, cursor);
            cursor = result.cursor;
            if (result.gotResultEvent) break;

            // Stream closed without firmware event — check server status
            if (!state.session) break;
            try {
                const stRes = await fetch(
                    `${API}/api/session/firmware/agent/status?session=${encodeURIComponent(state.session)}`
                );
                if (!stRes.ok) break;
                const stData = await stRes.json();
                if (stData.status !== 'running') {
                    loadFirmwareResult();
                    break;
                }
                if (attempt < MAX_RECONNECTS) {
                    if (agentSt) agentSt.textContent = 'Reconnecting…';
                    await new Promise(r => setTimeout(r, 1000));
                }
            } catch {
                break;
            }
        }
    } catch (e) {
        _appendMessage('error', `Connection error: ${e.message}`);
    } finally {
        _agentSending = false;
        if (input)   input.disabled  = false;
        if (btnSend) btnSend.disabled = false;
        if (btnGen)  btnGen.disabled  = false;
        if (agentSt) agentSt.textContent = '';
    }
}

// ── SSE event consumer ────────────────────────────────────────────

/**
 * @param {Response} response
 * @param {number} initialCursor
 * @returns {{ cursor: number, gotResultEvent: boolean }}
 */
async function _consumeSSE(response, initialCursor = 0) {
    const agentSt = document.getElementById('firmware-agent-status');
    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    let thinkingPre     = null;
    let messageBubble   = null;
    let messageBubbleText = '';
    let currentBlock    = null;
    let toolGroup       = null;
    let toolGroupItems  = null;

    let cursor = initialCursor;
    let gotResultEvent = false;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();

        for (const part of parts) {
            if (!part.trim()) continue;

            let eventType = 'message';
            let dataStr   = '';
            for (const line of part.split('\n')) {
                if (line.startsWith('event: '))      eventType = line.slice(7).trim();
                else if (line.startsWith('data: '))  dataStr  += line.slice(6);
                else if (line.startsWith('data:'))   dataStr  += line.slice(5);
            }

            let data = {};
            try { data = dataStr ? JSON.parse(dataStr) : {}; } catch { data = {}; }

            cursor++;

            switch (eventType) {
                case 'thinking_start':
                    currentBlock   = 'thinking';
                    toolGroup      = null;
                    toolGroupItems = null;
                    thinkingPre    = _createThinkingBubble();
                    if (agentSt) agentSt.textContent = 'Thinking…';
                    break;

                case 'thinking_delta':
                    if (thinkingPre && data.text) {
                        thinkingPre.textContent += data.text;
                        _scrollToBottom();
                    }
                    break;

                case 'message_start':
                    currentBlock      = 'message';
                    toolGroup         = null;
                    toolGroupItems    = null;
                    messageBubble     = _createMessageBubble();
                    messageBubbleText = '';
                    if (agentSt) agentSt.textContent = '';
                    break;

                case 'message_delta':
                    if (messageBubble && data.text) {
                        messageBubbleText += data.text;
                        messageBubble.innerHTML = _renderMarkdown(messageBubbleText);
                        _scrollToBottom();
                    }
                    break;

                case 'block_stop':
                    if (currentBlock === 'thinking') thinkingPre = null;
                    else if (currentBlock === 'message') {
                        messageBubble     = null;
                        messageBubbleText = '';
                    }
                    currentBlock = null;
                    break;

                case 'tool_call': {
                    if (!toolGroup) {
                        const g  = _createToolGroup();
                        toolGroup      = g.details;
                        toolGroupItems = g.items;
                    }
                    _appendToolItem(toolGroupItems, data.name, data.input);
                    if (agentSt) agentSt.textContent = `Calling ${data.name}…`;
                    break;
                }

                case 'tool_result':
                    if (toolGroupItems) _appendToolItemResult(toolGroupItems, data.name);
                    if (agentSt) agentSt.textContent = 'Thinking…';
                    break;

                case 'firmware':
                    gotResultEvent = true;
                    // Agent submitted a sketch — update preview
                    if (data.sketch) {
                        _renderSketchPreview(data.sketch);
                        document.getElementById('btn-download-firmware').hidden = false;
                        _appendFirmwareResult(data.sketch);
                    }
                    if (agentSt) agentSt.textContent = 'Firmware ready';
                    break;

                case 'error':
                    _appendMessage('error', data.message || 'Unknown error');
                    if (agentSt) agentSt.textContent = 'Error';
                    break;

                case 'done':
                    if (agentSt) agentSt.textContent = '';
                    break;
            }
        }
    }

    return { cursor, gotResultEvent };
}

// ── Render saved conversation history ────────────────────────────

function _renderConversation(messages) {
    for (const msg of messages) {
        if (msg.role === 'user') {
            const text = typeof msg.content === 'string'
                ? msg.content
                : (msg.content?.find?.(b => b.type === 'text')?.text ?? '');
            if (text) _appendMessage('user', text);
        } else if (msg.role === 'assistant') {
            _renderAssistantBlocks(msg.content);
        }
    }
}

function _renderAssistantBlocks(blocks) {
    if (!Array.isArray(blocks)) return;
    let toolItems = [];

    const flushTools = () => {
        if (!toolItems.length) return;
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
        _chatMessages().appendChild(div);
        toolItems = [];
    };

    for (const block of blocks) {
        switch (block.type) {
            case 'thinking':
                flushTools();
                if (block.thinking) {
                    const pre = _createThinkingBubble(false);
                    pre.textContent = block.thinking;
                }
                break;
            case 'text':
                flushTools();
                if (block.text) {
                    const div = _createMessageBubble();
                    div.innerHTML = _renderMarkdown(block.text);
                }
                break;
            case 'tool_use':
                toolItems.push(_makeStaticToolItem(block.name, block.input));
                break;
        }
    }
    flushTools();
}

// ── Result renderers ──────────────────────────────────────────────

function _renderSketchPreview(sketch) {
    const preview = document.getElementById('firmware-preview');
    if (!preview) return;
    const lines  = sketch.split('\n');
    const shown  = lines.slice(0, MAX_PREVIEW_LINES);
    const numbered = shown.map((line, i) =>
        `<span class="fw-line-num">${String(i + 1).padStart(3)}</span> ${_escapeHtml(line)}`
    ).join('\n');
    const truncMsg = lines.length > MAX_PREVIEW_LINES
        ? `\n<span class="fw-truncated">// … ${lines.length - MAX_PREVIEW_LINES} more lines — download for full sketch</span>`
        : '';
    preview.innerHTML = numbered + truncMsg;
    preview.hidden = false;
}

function _appendFirmwareResult(sketch) {
    const lineCount = sketch.split('\n').length;
    const div = document.createElement('div');
    div.className = 'chat-bubble design-result';
    div.innerHTML = `
        <div class="design-summary">
            <strong>✅ Firmware saved</strong>
            <span>${lineCount} lines · firmware.ino ready to download</span>
        </div>`;
    _chatMessages().appendChild(div);
    _scrollToBottom();
}

// ── DOM helpers ───────────────────────────────────────────────────

function _chatMessages() {
    return document.getElementById('firmware-chat-messages');
}

function _appendMessage(role, text) {
    const div = document.createElement('div');
    div.className = `chat-bubble ${role}`;
    div.textContent = text;
    _chatMessages().appendChild(div);
    _scrollToBottom();
}

function _createThinkingBubble(open = true) {
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
    _chatMessages().appendChild(div);
    _scrollToBottom();
    return pre;
}

function _createMessageBubble() {
    const div = document.createElement('div');
    div.className = 'chat-bubble assistant';
    _chatMessages().appendChild(div);
    _scrollToBottom();
    return div;
}

function _createToolGroup() {
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
    _chatMessages().appendChild(div);
    _scrollToBottom();
    return { details, items };
}

function _appendToolItem(container, name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    item.dataset.toolName = name;
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v.slice(0, 60) : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">${_escapeHtml(name)}</span>${_escapeHtml(inputStr)}`;
    container.appendChild(item);
    const summary = container.parentElement.querySelector('.tool-group-header');
    const count = container.children.length;
    summary.innerHTML = `<span class="tool-icon">🔧</span> ${count} tool call${count > 1 ? 's' : ''}`;
    _scrollToBottom();
}

function _appendToolItemResult(container, name) {
    const items = container.querySelectorAll(`.tool-item[data-tool-name="${name}"]`);
    const item = items[items.length - 1];
    if (!item) return;
    const nameSpan = item.querySelector('.tool-name');
    if (nameSpan) nameSpan.textContent = `✓ ${name}`;
}

function _makeStaticToolItem(name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v.slice(0, 60) : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">✓ ${_escapeHtml(name)}</span>${_escapeHtml(inputStr)}`;
    return item;
}

function _scrollToBottom() {
    const c = _chatMessages();
    if (!c) return;
    const atBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
    if (atBottom) c.scrollTop = c.scrollHeight;
}

function downloadFirmware() {
    if (!state.session) return;
    window.open(
        `${API}/api/session/firmware/download?session=${encodeURIComponent(state.session)}`,
        '_blank',
    );
}

// ── Markdown renderer (same lightweight approach as circuit.js) ──

function _renderMarkdown(text) {
    const lines = text.split('\n');
    const out = [];
    let inCode = false;
    let codeLines = [];
    let inUl = false, inOl = false;

    const closeList = () => {
        if (inUl) { out.push('</ul>'); inUl = false; }
        if (inOl) { out.push('</ol>'); inOl = false; }
    };

    for (const rawLine of lines) {
        const line = rawLine;

        if (inCode) {
            if (line.startsWith('```')) {
                out.push(`<pre><code>${_escapeHtml(codeLines.join('\n'))}</code></pre>`);
                codeLines = []; inCode = false;
            } else { codeLines.push(line); }
            continue;
        }
        if (line.startsWith('```')) { closeList(); inCode = true; continue; }

        if (/^#{1,3} /.test(line)) {
            closeList();
            const level = line.match(/^(#+)/)[1].length;
            const content = _inlineMarkdown(line.replace(/^#+\s+/, ''));
            out.push(`<h${level + 2}>${content}</h${level + 2}>`);
            continue;
        }

        if (/^\s*[-*] /.test(line)) {
            if (!inUl) { closeList(); out.push('<ul>'); inUl = true; }
            out.push(`<li>${_inlineMarkdown(line.replace(/^\s*[-*]\s+/, ''))}</li>`);
            continue;
        }
        if (/^\s*\d+\. /.test(line)) {
            if (!inOl) { closeList(); out.push('<ol>'); inOl = true; }
            out.push(`<li>${_inlineMarkdown(line.replace(/^\s*\d+\.\s+/, ''))}</li>`);
            continue;
        }

        closeList();
        if (line.trim() === '') { out.push('<br>'); continue; }
        out.push(`<p>${_inlineMarkdown(line)}</p>`);
    }
    closeList();
    if (inCode) out.push(`<pre><code>${_escapeHtml(codeLines.join('\n'))}</code></pre>`);
    return out.join('');
}

function _inlineMarkdown(text) {
    return _escapeHtml(text)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function _escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}
