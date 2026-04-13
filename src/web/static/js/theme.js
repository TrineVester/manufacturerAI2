const STORAGE_KEY = 'mfg-theme';
const MODEL_KEY   = 'mfg-model';

export function initThemeSwitcher() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) applyTheme(saved);

    const container = document.getElementById('theme-switcher');
    if (container) {
        container.addEventListener('click', e => {
            const btn = e.target.closest('.theme-btn');
            if (!btn) return;
            const theme = btn.dataset.theme;
            applyTheme(theme);
        });
    }

    // Gear icon dropdown toggle
    const gear = document.getElementById('settings-gear');
    const dropdown = document.getElementById('settings-dropdown');
    if (gear && dropdown) {
        gear.addEventListener('click', e => {
            e.stopPropagation();
            dropdown.hidden = !dropdown.hidden;
        });
        // Close dropdown on outside click
        document.addEventListener('click', e => {
            if (!dropdown.hidden && !dropdown.contains(e.target) && e.target !== gear) {
                dropdown.hidden = true;
            }
        });
    }

    // Model selector
    const modelSelect = document.getElementById('model-select');
    if (modelSelect) {
        const savedModel = localStorage.getItem(MODEL_KEY);
        if (savedModel) modelSelect.value = savedModel;
        modelSelect.addEventListener('change', () => {
            localStorage.setItem(MODEL_KEY, modelSelect.value);
        });
    }
}

/** Get the currently selected model name. */
export function getSelectedModel() {
    return localStorage.getItem(MODEL_KEY) || 'claude-sonnet-4-6';
}

function applyTheme(name) {
    if (name === 'midnight') {
        document.body.removeAttribute('data-theme');
    } else {
        document.body.setAttribute('data-theme', name);
    }
    localStorage.setItem(STORAGE_KEY, name);

    document.querySelectorAll('.theme-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === name);
    });
}
