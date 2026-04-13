# Update ManufacturerAI2 — Implementation Plan

This plan identifies concrete improvements to bring into manufacturerAI2 from the original manufacturerAI and its frontend. It is based on a deep audit of all three codebases (manufacturerAI2, manufacturerAI, manufacturerAI-Frontend) and documents the actual bugs found.

---

## Phase 0: Fix Existing Bugs (Do First — Nothing Else Matters Until These Are Fixed)

These are real bugs in manufacturerAI2 causing hangs, data corruption, and silent failures.

### 0.1 — Race Condition: Global `_stl_compile` Dict (CRITICAL)
**File:** `src/web/server.py` line 25  
**Bug:** The global `_stl_compile: dict[str, dict] = {}` is unprotected. Multiple concurrent `/api/session/scad/compile` requests can overwrite each other's state.  
**Fix:** Use `asyncio.Lock` or per-session locks around all reads/writes to `_stl_compile`.

### 0.2 — Session Save Race Condition (CRITICAL)
**File:** `src/session.py`  
**Bug:** No file locking on `session.save()`. Two concurrent requests can load the same session.json, make different changes, and the second write silently overwrites the first.  
**Example:** Request A marks placement=complete, Request B marks design=complete → B overwrites A's placement status.  
**Fix:** Add file locking (`fcntl`/`msvcrt` or `filelock` library) around session.json reads and writes. Or batch all state changes into a single save at end of request.

### 0.3 — Artifact/Session State Desync (CRITICAL)
**File:** `src/agent/core.py` lines 289-299  
**Bug:** Design submission deletes downstream artifacts, then saves session.json. If an exception occurs between delete and save, artifacts are gone but pipeline_state still says "complete."  
**Fix:** Make downstream invalidation atomic — update pipeline_state BEFORE deleting artifacts, then save, then delete files. Or use a transaction-like pattern (write new state first, then clean up).

### 0.4 — Double-Submit SSE Race in Frontend (HIGH)
**File:** `src/web/static/js/design.js` ~line 141  
**Bug:** No guard against clicking "Send" twice. Two parallel SSE streams corrupt the conversation.  
**Fix:** Disable send button while streaming. Add an `_isSending` flag that blocks duplicate submissions.

### 0.5 — Silent Error Swallowing (HIGH)
**Files:** Multiple locations  
- `src/agent/core.py` ~line 141: bare `except Exception: pass` on token counting
- `src/web/server.py` ~line 334, 349: bare except in design enrichment
- Token counting failure returns `{input_tokens: 0}` to frontend with no error flag  
**Fix:** Log all exceptions. For token counting, return `{input_tokens: null, error: true}` so frontend can show "token count unavailable" instead of a frozen meter.

### 0.6 — Session Loading Crashes on Corruption (HIGH)
**File:** `src/session.py`  
**Bug:** `load_session()` does `meta["id"]` without KeyError handling. One corrupted session.json crashes `list_sessions()` for ALL sessions.  
**Fix:** Wrap in try/except, skip corrupted sessions, log a warning.

### 0.7 — Design Enrichment Silently Returns Incomplete Data (MEDIUM)
**File:** `src/web/server.py` ~line 327-349  
**Bug:** If outline parsing fails in the enrichment function, it returns silently with no height_grid or surface_normal data. Frontend's 3D viewport renders incorrectly.  
**Fix:** Log the error. Return an `enrichment_error` flag in the response so the frontend knows the 3D data is incomplete.

---

## Phase 1: Agent Architecture — Split Design & Circuit (High Priority)

### 1.1 — Split Into Two Agents: DesignAgent + CircuitAgent

**Why this matters:** The current single-agent approach in manufacturerAI2 forces one LLM call to simultaneously solve:
- Shape geometry (CSG outline, corner easing, enclosure height)
- Component selection (which MCU, what resistor values)
- Electrical topology (which pins connect to what)
- UI placement (button/LED positions on surface)

This is too much for one prompt. The 1500-line mega-prompt mixes geometry rules with electrical rules, causing the model to context-switch constantly. The old manufacturerAI splits this cleanly:

**DesignAgent** ("You are a product designer"):
- Designs the physical shape (outline polygon, enclosure height, edge profiles)
- Places only UI components (buttons, LEDs, switches) — the things users interact with
- Does NOT pick internal components (MCU, resistors, batteries)
- Uses incremental `edit_design` tool (cheap iterations, not batch validation)
- Prompt focuses purely on aesthetics, ergonomics, and physical form

**CircuitAgent** ("You are an electronics engineer"):
- Receives the fixed design (outline + UI placements) as input
- Adds internal components (MCU, resistors, capacitors, batteries, power)
- Designs the net list (which pins connect to what)
- Uses pin group references (`mcu_1:gpio`) for dynamic allocation
- Prompt focuses purely on electrical correctness and manufacturability

**Benefits of splitting:**
- Each prompt is ~50% smaller → more room for iteration within token budget
- No circular dependency (design doesn't need to know circuit, circuit adapts to design)
- Independent retry (bad circuit? Re-run circuit agent without redoing physical design)
- Bidirectional feedback: if circuit can't fit, design agent gets feedback to widen the outline
- Matches human expertise boundaries (designer ≠ engineer)

**What to port from manufacturerAI:**
- The `_BaseAgent` class pattern (shared async loop, streaming, tool dispatch)
- The design agent prompt (form follows function, incremental editing, "NEVER compute geometry manually")
- The circuit agent prompt (electrical rules, pin group allocation, power discipline)
- The `edit_design` tool (find-replace on design JSON — much cheaper than full resubmission)
- The bidirectional feedback loop (circuit fails → design agent re-prompted to adjust)

### 1.2 — Improve Agent Prompts (for both agents)

Port these proven rules from the old system:

**For DesignAgent:**
- "Work incrementally — submit rough geometry, iterate from validation errors"
- "NEVER manually compute geometry — let the validator catch violations"
- "Form follows function — shape communicates device purpose"
- Only place components the user explicitly requested
- Add physical-world grounding: real component sizes, mounting constraints, edge clearance

**For CircuitAgent:**
- Use exact `instance_ids` from designer's UI placements
- Pin group references (`mcu_1:gpio`) instead of individual pins
- Every component must be powered (VCC + GND)
- Descriptive net names (VCC, GND, BTN1_IN, LED1_ANODE)
- Resistor value calculations (LED current limiting, pull-ups)
- Button: always INPUT_PULLUP + 50ms debounce
- MCU runs at 8MHz internal oscillator

### 1.3 — Remove Artificial Iteration Limits

**Bug:** manufacturerAI2 has `_MAX_FEASIBILITY_ATTEMPTS = 3`, after which the agent is told "STOP and ask the user." This is too aggressive.  
**Fix:** Remove the hard cap. Let the agent iterate until it runs out of token budget naturally. The old system has no such cap and works fine.

---

## Phase 2: Server & Data Stability

### 2.1 — Atomic Pipeline State Updates
**Problem:** Pipeline state (session.json) and artifact files (design.json, placement.json, etc.) can get out of sync because they're written separately.  
**What to do:**
- Update pipeline_state dict FIRST, save session.json, THEN write/delete artifact files
- If artifact write fails, revert pipeline_state
- Add a `session.batch_update()` context manager that collects all changes and writes once at the end

### 2.2 — Pipeline Error Tracking
**Problem:** When placement/routing/SCAD fails, there's no record of what went wrong or which upstream stage caused it.  
**Source:** manufacturerAI tracks `pipeline_errors: {stage: {error, reason, responsible_agent}}`.  
**What to do:**
- Add `pipeline_errors` dict to Session model
- Record structured errors when each stage fails
- Surface these in the UI (e.g., "Routing failed: components too close together. The design agent may need to widen the outline.")

### 2.3 — Frontend State Consistency
**Problem:** The JS frontend can show stale data after pipeline changes.  
**What to do:**
- After any pipeline stage completes, refresh the session state from the server
- Add a `version` counter to session.json that increments on every save
- Frontend checks version on each API response and refetches if stale
- Disable "Send" button while an SSE stream is active (prevent double-submit)

---

## Phase 3: Router Improvements

### 3.1 — Port GA-Inspired Router Retry
**Problem:** manufacturerAI2's router gives up too easily and has no intelligent retry.  
**Source:** manufacturerAI has a proven iterative improvement loop.  
**What to do:**
- Port the **elite pool** (track best 5 routing solutions, restore and mutate from them)
- Port the **phase rotation**: refine (rip-up worst nets) → restart (shuffle all) → crossover (restore elite) → explore (perturb neighborhood)
- Port **stall detection** with dynamic limits (3× multiplier when nets are unrouted)
- Port **DRC repair pass** (5 rounds of post-routing clearance violation repair using cost maps around offending pins)
- Port **pin re-allocation** during retries — if a net won't route, try different physical pins from the dynamic pool

### 3.2 — Safer Physical Constants
**File:** `src/pipeline/config.py`  
**Problem:** Current values are borderline for real manufacturing:
- `trace_clearance_mm = 1.5` → silver ink tolerance ±0.2mm, two traces could touch
- `edge_clearance_mm = 1.5` → FDM prints have ±0.5mm tolerance on outlines  
**Fix:** Increase `trace_clearance_mm` to 2.0, `edge_clearance_mm` to 2.5. Or make these configurable per-session based on printer accuracy.

---

## Phase 4: SCAD & Physical Output Improvements

### 4.1 — SCAD Cutout Improvements
**Problem:** manufacturerAI2 has basic cutouts. The old system has manufacturing-grade details.  
**Source:** manufacturerAI's SCAD fragment system.  
**What to do:**
- Port **pin taper/funnel mouths** (1.0-1.5mm graduated funnels) — makes component insertion much easier during assembly
- Port **button snap-fit generation** (socket + stem + cap geometry) — buttons need this to work physically
- Port **cutout merging by z-layer** — group cutouts by (z_base, depth), Shapely union overlapping ones, reduce CSG complexity and OpenSCAD render time
- Port **support platforms** for internal components that sit at a specific height
- Return OpenSCAD error output to the user (currently written to a log file the user can't access)

### 4.2 — GCode Pipeline
**Problem:** manufacturerAI2 has no slicing or GCode generation — can't go from STL to print.  
**Source:** manufacturerAI has a 5-step GCode pipeline.  
**What to do:**
- Port PrusaSlicer integration with printer profiles
- Port filament definitions (PLA, PETG, TPU, ABS, Nylon with temps/cooling)
- Port pause point insertion (ink pause at Z=2mm, component insertion pauses)
- Port ironing block insertion for smooth ink layer
- Port bed offset calculation from STL bounding box
- Port manufacturing manifest generation

### 4.3 — Bitmap Generation
**Problem:** manufacturerAI2 doesn't generate ink printer bitmaps.  
**Source:** manufacturerAI generates 1-bit bitmaps at Xaar 128 nozzle pitch (0.1371mm).  
**What to do:**
- Port bitmap rasterizer (trace polygons → 1-bit bitmap at nozzle pitch)
- Add bitmap preview in the manufacturing panel

---

## Phase 5: Frontend Upgrades

### 5.1 — 2D Placement & Routing Viewports
**Problem:** Users can't see component placement or trace routing visually.  
**Source:** manufacturerAI-Frontend has SVG-based viewports.  
**What to do:**
- SVG placement view: outline polygon + positioned components with rotation + labels
- SVG routing view: traces with per-net color coding, hover to highlight a net
- Port the `ComponentIcon` SVG renderer for 2D component symbols

### 5.2 — 3D Preview with Three.js
**Problem:** No 3D preview — users can't see what they're building.  
**Source:** manufacturerAI-Frontend has a Three.js STL viewer.  
**What to do:**
- Add Three.js (CDN)
- STL loader + OrbitControls + ambient/directional lighting
- Display in the SCAD panel after STL generation
- Auto-frame camera from model bounding box

### 5.3 — Improved Chat UI
**Problem:** Basic chat with no tool call inspection.  
**Source:** manufacturerAI-Frontend has collapsible tool groups, markdown rendering.  
**What to do:**
- Collapsible tool call/result groups (collapsed by default)
- Markdown rendering for agent messages
- Warning ring on token meter at 70%/90%
- Visual distinction for thinking blocks

### 5.4 — Theme System
**Source:** manufacturerAI-Frontend has HSL-based theme system with 60+ CSS variables.  
**What to do:**
- Port CSS variable color system (surface, text, accent, status)
- Add a few preset color schemes
- Low priority, pure polish

---

## Phase 6: Advanced Features (Future)

### 6.1 — Assembly Guide
- Generate step-by-step assembly instructions from placement data
- Group by component type, show coordinates and orientation

### 6.2 — Firmware Generation & Simulation
- Port SetupAgent for Arduino sketch generation
- Port ATmega328P pin mapping
- Port device simulator (interactive 3D with buttons/LEDs)
- Stretch goal — large feature

### 6.3 — Catalog Enhancements
- Pin shapes (rect/slot) for accurate pad geometry
- Body channels (cylindrical cutouts for battery holders)
- Extra parts (separate printed pieces like button caps)

---

## Known Bugs in Old manufacturerAI (DO NOT PORT These)

These bugs exist in the old system and must NOT be carried over:

| Bug | Old System File | Description |
|-----|-----------------|-------------|
| Session save race | `session.py` | `write_artifact()` calls `save()` on EVERY artifact write, creating race windows |
| Stale frontend cache | `PipelineContext.tsx` | Cached circuit/design objects not cleared on invalidation events |
| Agent overwrites manual edits | `routes/design.py` | Manual design edits don't update agent conversation → agent reverts changes |
| Pending artifact leak | `session.py` | `circuit_pending.json` not cleaned up on upstream invalidation |
| Silent file write failures | `session.py` | No try/except on `write_text()` — agent thinks save succeeded when it didn't |
| Corrupted session crashes list | `session.py` | One bad `session.json` crashes `list_sessions()` for all sessions |
| Non-deterministic invalidation | `session.py` | `None` vs `{}` hash differently in component signatures |

---

## Implementation Order

| Order | Item | Effort | Impact |
|-------|------|--------|--------|
| 1 | Phase 0 — Bug fixes | Small | CRITICAL — everything else is unstable without this |
| 2 | 1.1 Split into Design + Circuit agents | Large | High — core architecture improvement |
| 3 | 1.2 Improve agent prompts | Medium | High — fewer failed designs |
| 4 | 1.3 Remove artificial iteration limits | Small | Medium — less frustrating failures |
| 5 | 2.1-2.3 Server & data stability | Medium | High — no more "weird data updates" |
| 6 | 3.1 Router retry logic | Medium | High — fewer routing failures |
| 7 | 3.2 Safer physical constants | Small | Medium — better physical parts |
| 8 | 4.1 SCAD cutout improvements | Medium | High — usable physical parts |
| 9 | 5.1 2D placement/routing viewports | Medium | High — users can see results |
| 10 | 5.3 Improved chat UI | Small | Medium — better UX |
| 11 | 5.2 3D preview | Medium | High — visual impact |
| 12 | 4.2 GCode pipeline | Large | High — end-to-end manufacturing |
| 13 | 4.3 Bitmap generation | Medium | High — needed for printing |
| 14 | 5.4 Theme system | Small | Low — polish |
| 15 | 6.1 Assembly guide | Small | Medium — user guidance |
| 16 | 6.2 Firmware & simulation | Very Large | Medium — full workflow |
| 17 | 6.3 Catalog enhancements | Medium | Low — incremental |

---

## Key Principles

1. **Fix bugs before adding features** — the current codebase has race conditions and silent failures that will corrupt any new features built on top
2. **Split the agent** — one brain can't be a sculptor and an engineer simultaneously; two focused agents with smaller prompts produce better results and are cheaper to iterate
3. **Port, don't rewrite** — the old code works, it just needs to be cleaned up and integrated without bringing over the bugs listed above
4. **Keep the single-server approach** — FastAPI + static files is simpler to deploy than the old Next.js split; port frontend features as vanilla JS/CSS
5. **Atomic state updates** — never let session.json and artifact files get out of sync
6. **No silent failures** — every exception must be logged and surfaced to the user
