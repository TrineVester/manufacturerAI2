/* Pipeline progress bar — step-done helpers.
 * Kept in its own module to avoid circular imports. */

function _stepBtn(step) {
    return document.querySelector(`#pipeline-steps .step[data-step="${step}"]`);
}

function _updateProgress() {
    const steps = document.querySelectorAll('#pipeline-steps .step[data-step]');
    const done = [...steps].filter(b => b.classList.contains('step-done')).length;
    const fill = document.getElementById('pipeline-progress-fill');
    if (fill) fill.style.width = `${(done / steps.length) * 100}%`;
}

/** Mark a pipeline step as completed (advances the progress bar). */
export function markStepDone(step) {
    const btn = _stepBtn(step);
    if (btn) btn.classList.add('step-done');
    _updateProgress();
}

/** Remove the done mark from one or more pipeline steps (shrinks the progress bar). */
export function markStepUndone(...steps) {
    for (const step of steps) {
        const btn = _stepBtn(step);
        if (btn) btn.classList.remove('step-done');
    }
    _updateProgress();
}

/** Call once on page load to set the initial bar width. */
export function initPipelineProgress() {
    _updateProgress();
}
