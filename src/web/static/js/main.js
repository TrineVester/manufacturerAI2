/* Entry point — wires DOM events and kicks off initial load */

import { API, state } from './state.js';
import { closeModal } from './utils.js';
import { setSessionLabel, startNewSession, showSessionsModal, setSessionUrl } from './session.js';
import { loadCatalog, reloadCatalog } from './catalog.js';
import { sendDesignPrompt, loadConversation } from './design.js';
import { sendCircuitPrompt, loadCircuitConversation, enableCircuitTab } from './circuit.js';
import { runPlacement, loadPlacementResult, enablePlacementTab } from './placement.js';
import { runRouting, loadRoutingResult, enableRoutingTab } from './routing.js';
import { runScad, loadScadResult, enableScadTab } from './scad.js';
import { runManufacturing, loadManufacturingResult, enableManufacturingTab, initManufacturingConfig, syncManufacturingConfig } from './manufacturing.js';
import { initFirmwarePanel, loadFirmwareResult, showFirmwareSection } from './firmware.js';
import { initGuide, openGuide, enableGuideBtn } from './guide.js';
import { initPipelineProgress, markStepDone, markStepUndone } from './pipelineProgress.js';
import { initThemeSwitcher } from './theme.js';
import { setStep } from './viewport.js';
import './viewportDesign.js';   // registers the design viewport handler
import './viewportCircuit.js';   // registers the circuit viewport handler
import './viewportPlacement.js'; // registers the placement viewport handler
import './viewportRouting.js';   // registers the routing viewport handler
import './viewportScad.js';      // registers the SCAD / STL viewport handler
import './viewportManufacturing.js'; // registers the manufacturing viewport handler

document.addEventListener('DOMContentLoaded', () => {
    // Restore session from URL
    const params = new URLSearchParams(window.location.search);
    state.session = params.get('session');
    if (state.session) {
        setSessionLabel(state.session);
        loadConversation();
        loadCircuitConversation();
        loadPlacementResult();    // load existing placement if present
        loadRoutingResult();      // load existing routing if present
        loadScadResult();         // load existing SCAD if present
        loadManufacturingResult(); // load existing manufacturing if present
        loadFirmwareResult();      // load existing firmware if present
        // Fetch session name for the label; clear URL if session no longer exists
        fetch(`${API}/api/session?session=${encodeURIComponent(state.session)}`)
            .then(r => {
                if (r.status === 404) {
                    startNewSession();
                    return null;
                }
                return r.ok ? r.json() : null;
            })
            .then(data => {
                if (data?.name) setSessionLabel(state.session, data.name);
                // Sync printer/filament dropdowns from session
                syncManufacturingConfig(data);
                // Enable circuit nav if design is complete
                if (data?.artifacts?.design) {
                    enableCircuitTab(!data?.artifacts?.circuit_conversation);
                }
                // Enable placement nav if circuit is complete (or design if no circuit)
                if (data?.artifacts?.design) {
                    enablePlacementTab(!data?.artifacts?.placement);
                }
                // Enable routing nav if placement is complete
                if (data?.artifacts?.placement) {
                    enableRoutingTab(!data?.artifacts?.routing);
                }
                // Enable SCAD nav if routing is complete
                if (data?.artifacts?.routing) {
                    enableScadTab(!data?.artifacts?.scad);
                }
                // Enable manufacturing nav if SCAD is complete
                if (data?.artifacts?.scad) {
                    enableManufacturingTab(!data?.artifacts?.gcode);
                }
                // Show firmware section if routing is complete
                if (data?.artifacts?.routing) {
                    showFirmwareSection();
                }
                // Enable guide if placement is complete
                if (data?.artifacts?.placement) {
                    enableGuideBtn(true);
                }
            })
            .catch(() => {});
    }

    // Pipeline nav (step buttons only — not catalog/sessions)
    document.querySelectorAll('#pipeline-nav .step[data-step]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.disabled) return;
            switchStep(btn.dataset.step);
        });
    });

    // Initialize viewport with active step
    setStep(state.activeStep || 'design');

    // Initialize guide controls
    initGuide();

    // Initialize theme switcher
    initThemeSwitcher();

    // Set initial pipeline progress bar width
    initPipelineProgress();

    // Initialize firmware panel
    initFirmwarePanel();

    // Initialize manufacturing config (printer/filament selects)
    initManufacturingConfig();

    // Header buttons
    document.getElementById('btn-new-session').addEventListener('click', startNewSession);

    // Nav-bar: Sessions (right side)
    document.getElementById('btn-list-sessions').addEventListener('click', showSessionsModal);

    // Nav-bar: Guide (right side)
    document.getElementById('btn-guide').addEventListener('click', openGuide);

    // Nav-bar: Catalog toggle (right side)
    document.getElementById('btn-catalog').addEventListener('click', () => {
        const catalogPanel = document.getElementById('step-catalog');
        const catalogBtn = document.getElementById('btn-catalog');
        const isVisible = !catalogPanel.hidden;
        if (isVisible) {
            catalogPanel.hidden = true;
            catalogBtn.classList.remove('active');
            // Restore the active pipeline step
            const activeStep = state.activeStep || 'design';
            const activePanel = document.getElementById(`step-${activeStep}`);
            if (activePanel) activePanel.hidden = false;
            // Re-highlight the active pipeline nav button
            document.querySelectorAll('#pipeline-nav .step[data-step]').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.step === activeStep);
            });
        } else {
            // Hide all step panels, show catalog
            document.querySelectorAll('.step-panel').forEach(p => p.hidden = true);
            catalogPanel.hidden = false;
            if (!state.catalog) loadCatalog();
            // Deselect pipeline buttons, highlight catalog
            document.querySelectorAll('#pipeline-nav .step[data-step]').forEach(btn => {
                btn.classList.remove('active');
            });
            catalogBtn.classList.add('active');
        }
    });
    document.getElementById('btn-reload-catalog').addEventListener('click', reloadCatalog);

    // Placement
    document.getElementById('btn-run-placement').addEventListener('click', runPlacement);

    // Routing
    document.getElementById('btn-run-routing').addEventListener('click', runRouting);

    // SCAD
    document.getElementById('btn-run-scad').addEventListener('click', runScad);

    // Manufacturing
    document.getElementById('btn-run-manufacturing').addEventListener('click', runManufacturing);

    // Design chat
    document.getElementById('btn-send-design').addEventListener('click', sendDesignPrompt);
    document.getElementById('chat-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendDesignPrompt();
        }
    });

    // Circuit chat
    document.getElementById('btn-send-circuit').addEventListener('click', sendCircuitPrompt);
    document.getElementById('circuit-chat-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendCircuitPrompt();
        }
    });

    // Modal close buttons
    document.querySelectorAll('.modal-close').forEach(btn => {
        btn.addEventListener('click', () => closeModal(btn.closest('.modal')));
    });

    // Backdrop click closes modal
    document.querySelectorAll('.modal').forEach(modal => {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeModal(modal);
        });
    });
});

/** Mark a pipeline step as completed (advances the progress bar). */
export { markStepDone, markStepUndone } from './pipelineProgress.js';

export function switchStep(step) {
    state.activeStep = step;
    document.querySelectorAll('#pipeline-nav .step[data-step]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.step === step);
        // Stop flashing when user clicks the tab
        if (btn.dataset.step === step) btn.classList.remove('tab-flash');
    });
    // Deselect catalog button
    document.getElementById('btn-catalog').classList.remove('active');
    document.querySelectorAll('.step-panel').forEach(panel => {
        panel.hidden = panel.id !== `step-${step}`;
    });
    // Sync viewport
    setStep(step);
    // Reload conversation + result so the tab reflects any background agent activity
    if (step === 'design') loadConversation();
    if (step === 'circuit') loadCircuitConversation();
    if (step === 'placement') loadPlacementResult();
    if (step === 'routing') loadRoutingResult();
    if (step === 'scad') loadScadResult();
    if (step === 'manufacturing') loadManufacturingResult();
    if (step === 'firmware') loadFirmwareResult();
}
