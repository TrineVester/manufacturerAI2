/* Shared mutable state + API config */

export const API = '';  // same origin

export const state = {
    session: null,    // current session ID (null = new/unsaved)
    catalog: null,    // cached catalog API response
    activeStep: 'design',
    sessionVersion: 0, // server-side version counter for staleness detection
};
