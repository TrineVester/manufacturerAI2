/* Design tab — chat interface for the LLM design agent (SSE streaming) */

import { API, state } from './state.js';
import { onSessionCreated, setSessionLabel } from './session.js';
import { setData as setViewportData, clearData as clearViewportData } from './viewport.js';
import { enablePlacementTab, resetPlacementPanel } from './placement.js';
import { resetRoutingPanel } from './routing.js';
import { enableCircuitTab, resetCircuitPanel, sendCircuitPrompt } from './circuit.js';
import { getSelectedModel } from './theme.js';
import { markStepDone, markStepUndone } from './pipelineProgress.js';

const messagesDiv = () => document.getElementById('chat-messages');
const statusSpan = () => document.getElementById('design-status');
let _isSending = false;  // Guard against double-submit

// ── Load conversation history ─────────────────────────────────────

/** Load and render saved conversation for the current session. */
export async function loadConversation() {
    const container = messagesDiv();
    if (!container) return;
    container.innerHTML = '';

    if (!state.session) return;

    try {
        const res = await fetch(
            `${API}/api/session/conversation?session=${encodeURIComponent(state.session)}`
        );
        if (res.ok) {
            const messages = await res.json();
            if (Array.isArray(messages) && messages.length > 0) {
                renderConversation(messages);
            }
        }
    } catch {
        // Silently ignore — empty chat is fine
    }

    // Always try to show the design result even if there is no conversation
    loadDesignResult();

    // Fetch current token count and update the meter
    if (state.session) {
        try {
            const res = await fetch(`${API}/api/session/tokens?session=${encodeURIComponent(state.session)}`);
            if (res.ok) {
                const t = await res.json();
                updateTokenMeter(t.input_tokens, t.budget);
            }
        } catch { /* best-effort */ }
    }
}

/**
 * Render a saved Anthropic-format message list into the chat UI.
 * Produces the same DOM structure as the live SSE stream.
 */
function renderConversation(messages) {
    for (const msg of messages) {
        if (msg.role === 'user') {
            if (typeof msg.content === 'string') {
                appendMessage('user', msg.content);
            } else if (Array.isArray(msg.content)) {
                // Extract user-visible text blocks, skipping injected context preambles
                for (const block of msg.content) {
                    if (block.type === 'text' && block.text &&
                        !block.text.startsWith('<!-- design-context -->') &&
                        !block.text.startsWith('<!-- circuit-context -->')) {
                        appendMessage('user', block.text);
                    }
                }
            }
        } else if (msg.role === 'assistant') {
            renderAssistantBlocks(msg.content);
        }
    }

}

/** Render an array of content blocks (thinking, text, tool_use). */
function renderAssistantBlocks(blocks) {
    if (!Array.isArray(blocks)) return;

    // Group tool_use blocks together
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

/** If this session has a design.json, render the design result box */
async function loadDesignResult() {
    if (!state.session) return;
    try {
        const res = await fetch(
            `${API}/api/session/design/result?session=${encodeURIComponent(state.session)}`
        );
        if (!res.ok) return;
        const design = await res.json();
        if (design && design.outline) {
            appendDesignResult(design);
            setViewportData('design', design);
            // Enable placement tab since design exists
            enablePlacementTab();
        }
    } catch {
        // No design yet — that's fine
    }
}

/** Send a design prompt and stream SSE events */
export async function sendDesignPrompt() {
    const input = document.getElementById('chat-input');
    const prompt = input.value.trim();
    if (!prompt || _isSending) return;

    _isSending = true;

    // Show user message and clear input
    appendMessage('user', prompt);
    input.value = '';
    input.disabled = true;
    document.getElementById('btn-send-design').disabled = true;
    statusSpan().textContent = 'Connecting…';

    try {
        // Build URL — session param is optional (server auto-creates if missing)
        let url = `${API}/api/session/design`;
        if (state.session) {
            url += `?session=${encodeURIComponent(state.session)}`;
        }

        // 1. POST to trigger the design agent
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

        const postData = await postRes.json();
        // Handle session creation for first message
        if (postData.session_id) {
            onSessionCreated(postData.session_id);
        }

        // 2. Open SSE stream — with reconnect on unexpected closure
        const streamBase = `${API}/api/session/design/stream`
            + (state.session ? `?session=${encodeURIComponent(state.session)}` : '');

        let cursor = 0;
        const MAX_RECONNECTS = 5;
        for (let attempt = 0; attempt <= MAX_RECONNECTS; attempt++) {
            const url = cursor > 0 ? `${streamBase}&after=${cursor}` : streamBase;
            let sseRes;
            try {
                sseRes = await fetch(url);
            } catch (fetchErr) {
                appendMessage('error', `Connection error: ${fetchErr.message}`);
                break;
            }
            if (!sseRes.ok) {
                appendMessage('error', `Failed to open event stream (${sseRes.status})`);
                break;
            }
            const result = await consumeSSE(sseRes, cursor);
            cursor = result.cursor;
            if (result.gotResultEvent) break;

            // Stream closed without delivering the design event — check server status
            if (!state.session) break;
            try {
                const stRes = await fetch(
                    `${API}/api/session/design/status?session=${encodeURIComponent(state.session)}`
                );
                if (!stRes.ok) break;
                const stData = await stRes.json();
                if (stData.status !== 'running') {
                    // Task finished while we were disconnected — recover from disk
                    loadDesignResult();
                    break;
                }
                if (attempt < MAX_RECONNECTS) {
                    // Task still running — reconnect after brief delay
                    statusSpan().textContent = 'Reconnecting…';
                    await new Promise(r => setTimeout(r, 1000));
                }
            } catch {
                break;
            }
        }
    } catch (e) {
        appendMessage('error', `Connection error: ${e.message}`);
    } finally {
        _isSending = false;
        input.disabled = false;
        document.getElementById('btn-send-design').disabled = false;
        statusSpan().textContent = '';
    }
}

// ── SSE parser ────────────────────────────────────────────────────

/**
 * Parse an SSE stream from a fetch Response.
 * We use fetch + ReadableStream instead of EventSource because
 * EventSource only supports GET requests.
 *
 * @param {Response} response
 * @param {number} initialCursor  - number of events already processed before this call
 * @returns {{ cursor: number, gotResultEvent: boolean }}
 */
async function consumeSSE(response, initialCursor = 0) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // Track current live-updating elements
    let thinkingPre = null;   // <pre> inside the thinking bubble
    let messageBubble = null; // <div> for the assistant text bubble
    let messageBubbleText = ''; // raw text buffer for markdown re-rendering
    let currentBlock = null;  // 'thinking' | 'message' | null
    let toolGroup = null;     // current tool group <details> element
    let toolGroupItems = null; // container for tool items inside the group

    let cursor = initialCursor;
    let gotResultEvent = false;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE messages are separated by double newlines
        const parts = buffer.split('\n\n');
        buffer = parts.pop(); // last part is incomplete

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

            cursor++;

            // ── Handle each event type ──

            switch (eventType) {
                case 'session_created':
                    onSessionCreated(data.session_id);
                    break;

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
                    if (currentBlock === 'thinking') {
                        thinkingPre = null;
                    } else if (currentBlock === 'message') {
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
                    gotResultEvent = true;
                    appendDesignResult(data.design);
                    setViewportData('design', data.design);
                    statusSpan().textContent = 'Design complete!';
                    markStepDone('design');
                    markStepUndone('placement', 'routing', 'scad', 'manufacturing');
                    // Enable circuit step now that design exists
                    enableCircuitTab(true);
                    // Also enable placement (can skip circuit)
                    enablePlacementTab(false);
                    // Invalidate downstream
                    resetCircuitPanel();
                    // Auto-run circuit agent now that a fresh design exists
                    sendCircuitPrompt();
                    resetPlacementPanel();
                    resetRoutingPanel();
                    // Disable routing, scad, manufacturing tabs until new placement/routing/scad
                    {
                        for (const s of ['routing', 'scad', 'manufacturing']) {
                            const b = document.querySelector(`#pipeline-nav .step[data-step="${s}"]`);
                            if (b) { b.disabled = true; b.classList.remove('tab-flash'); }
                        }
                    }
                    break;

                case 'error':
                    appendMessage('error', data.message || 'Unknown error');
                    statusSpan().textContent = 'Error';
                    break;

                case 'session_named':
                    if (data.name && state.session) {
                        setSessionLabel(state.session, data.name);
                    }
                    break;

                case 'token_usage':
                    updateTokenMeter(data.input_tokens, data.budget);
                    break;

                case 'done':
                    // Refresh token count from server after turn completes
                    // (includes any tool_result messages appended after last count)
                    if (state.session) {
                        fetch(`${API}/api/session/tokens?session=${encodeURIComponent(state.session)}`)
                            .then(r => r.ok ? r.json() : null)
                            .then(t => { if (t) updateTokenMeter(t.input_tokens, t.budget); })
                            .catch(() => {});
                    }
                    break;
            }
        }
    }

    return { cursor, gotResultEvent };
}

// ── Render helpers ────────────────────────────────────────────────

function appendMessage(role, text) {
    const div = document.createElement('div');
    div.className = `chat-bubble ${role}`;
    div.textContent = text;
    messagesDiv().appendChild(div);
    scrollToBottom();
}

/** Create an empty thinking bubble and return the <pre> for delta appending */
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

/** Create an empty assistant message bubble and return it for delta appending */
function createMessageBubble() {
    const div = document.createElement('div');
    div.className = 'chat-bubble assistant';
    messagesDiv().appendChild(div);
    scrollToBottom();
    return div;
}

/** Create a tool group container (collapsed <details>) and return refs */
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

/** Add a tool call entry inside a tool group */
function appendToolItem(container, name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    item.dataset.toolName = name;
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">${escapeHtml(name)}</span>${escapeHtml(inputStr)}`;
    container.appendChild(item);

    // Update summary count
    const summary = container.parentElement.querySelector('.tool-group-header');
    const count = container.children.length;
    summary.innerHTML = `<span class="tool-icon">🔧</span> ${count} tool call${count > 1 ? 's' : ''}`;
    scrollToBottom();
}

/** Append a result line to the most recent matching tool item */
function appendToolItemResult(container, name, content) {
    // Find the last tool item for this name
    const items = container.querySelectorAll(`.tool-item[data-tool-name="${name}"]`);
    const item = items[items.length - 1];
    if (!item) return;
    // Mark as done
    const nameSpan = item.querySelector('.tool-name');
    if (nameSpan) nameSpan.textContent = `✓ ${name}`;
}

/** Render tool calls from saved conversation (static, not streaming) */
function appendToolCallStatic(name, input) {
    const item = document.createElement('div');
    item.className = 'tool-item';
    const inputStr = input && Object.keys(input).length > 0
        ? `(${Object.values(input).map(v => typeof v === 'string' ? v : JSON.stringify(v)).join(', ')})`
        : '()';
    item.innerHTML = `<span class="tool-name">✓ ${escapeHtml(name)}</span>${escapeHtml(inputStr)}`;
    return item;
}

function appendDesignResult(design) {
    const div = document.createElement('div');
    div.className = 'chat-bubble design-result';

    const compCount = design.components?.length || 0;
    const netCount = design.nets?.length || 0;
    const vertCount = (Array.isArray(design.outline) ? design.outline : design.outline?.vertices)?.length || 0;

    div.innerHTML = `
        <div class="design-summary">
            <strong>✅ Design Validated</strong>
            <span>${compCount} components · ${netCount} nets · ${vertCount}-vertex outline</span>
        </div>
        <details>
            <summary>View design JSON</summary>
            <pre class="design-json">${escapeHtml(JSON.stringify(design, null, 2))}</pre>
        </details>
    `;
    messagesDiv().appendChild(div);
    scrollToBottom();
}

function scrollToBottom() {
    const container = messagesDiv();
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
    if (atBottom) container.scrollTop = container.scrollHeight;
}

// ── Markdown renderer (lightweight, XSS-safe) ─────────────────────

/**
 * Convert a subset of Markdown to safe HTML.
 * Handles: headings, **bold**, *italic*, `code`, ```code blocks```,
 * - unordered lists, 1. ordered lists, blank line breaks.
 */
function renderMarkdown(text) {
    const lines = text.split('\n');
    const out = [];
    let inUl = false;
    let inOl = false;
    let inCode = false;
    let codeLang = '';
    let codeLines = [];

    const closeUl = () => { if (inUl) { out.push('</ul>'); inUl = false; } };
    const closeOl = () => { if (inOl) { out.push('</ol>'); inOl = false; } };
    const closeLists = () => { closeUl(); closeOl(); };

    for (const line of lines) {
        // Fenced code blocks
        if (/^```/.test(line)) {
            if (inCode) {
                // Close code block
                out.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
                inCode = false;
                codeLines = [];
                codeLang = '';
            } else {
                closeLists();
                inCode = true;
                codeLang = line.slice(3).trim();
                codeLines = [];
            }
            continue;
        }
        if (inCode) {
            codeLines.push(line);
            continue;
        }

        // Headings
        const headingMatch = line.match(/^(#{1,4})\s+(.+)/);
        if (headingMatch) {
            closeLists();
            const level = headingMatch[1].length;
            // Render as h4-h6 to stay smaller than panel headings
            const tag = `h${Math.min(level + 3, 6)}`;
            out.push(`<${tag} class="md-heading">${inlineMarkdown(headingMatch[2])}</${tag}>`);
            continue;
        }

        // Unordered list
        if (/^[-*] /.test(line)) {
            closeOl();
            if (!inUl) { out.push('<ul>'); inUl = true; }
            out.push(`<li>${inlineMarkdown(line.slice(2))}</li>`);
            continue;
        }

        // Ordered list
        const olMatch = line.match(/^(\d+)[.)]\s+(.+)/);
        if (olMatch) {
            closeUl();
            if (!inOl) { out.push('<ol>'); inOl = true; }
            out.push(`<li>${inlineMarkdown(olMatch[2])}</li>`);
            continue;
        }

        // Default: paragraph or blank line
        closeLists();
        if (line.trim() === '') {
            out.push('<br>');
        } else {
            out.push(`<p>${inlineMarkdown(line)}</p>`);
        }
    }

    // Close any open blocks
    if (inCode && codeLines.length) {
        out.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    }
    closeLists();
    return out.join('');
}

function inlineMarkdown(raw) {
    // Escape HTML first to prevent XSS
    const esc = raw
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    // Then apply inline patterns
    return esc
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`(.+?)`/g, '<code>$1</code>');
}

function escapeHtml(text) {
    const el = document.createElement('div');
    el.textContent = text;
    return el.innerHTML;
}

// ── Token meter ───────────────────────────────────────────────────

function updateTokenMeter(inputTokens, budget) {
    const meter = document.getElementById('token-meter');
    const fill = meter?.querySelector('.token-pie-fill');
    const label = document.getElementById('token-label');
    if (!meter || !fill || !label) return;

    meter.hidden = false;

    const pct = Math.min(inputTokens / budget, 1);
    const dashLen = (pct * 100).toFixed(1);
    fill.setAttribute('stroke-dasharray', `${dashLen} 100`);

    // Color thresholds
    fill.classList.remove('warn', 'critical');
    if (pct >= 0.9) fill.classList.add('critical');
    else if (pct >= 0.7) fill.classList.add('warn');

    const usedK = (inputTokens / 1000).toFixed(1);
    const budgetK = (budget / 1000).toFixed(0);
    label.textContent = `${usedK}k / ${budgetK}k`;
}
