"""Firmware agent — LLM-powered Arduino sketch generation.

Subclasses _BaseAgent to write a complete .ino sketch from routed PCB data.
The single terminal tool is submit_firmware(code), which saves the sketch and
ends the agent loop.  Follow-up chat turns allow iterative refinement.
"""

from __future__ import annotations

from src.agent.core import _BaseAgent, AgentEvent
from src.agent.tools import SETUP_TOOLS
from src.pipeline.firmware.context_builder import build_firmware_context


def build_firmware_user_prompt(design: dict) -> str:
    """Build the initial user message sent to the firmware agent on the first turn."""
    desc = design.get("device_description", "this device")
    return (
        f"Write the complete Arduino firmware for: {desc}\n\n"
        "Implement behaviour that matches the device description and component roles "
        "listed in your system prompt. Use only the pin numbers from the pin map — "
        "never invent pins. Call submit_firmware with the complete .ino file when ready."
    )


class FirmwareAgent(_BaseAgent):
    """Writes and revises Arduino .ino sketches based on routed PCB data.

    Tools
    -----
    submit_firmware(code) : terminal
        Validates the sketch is non-empty, saves it to firmware.ino, and marks
        the firmware stage complete.  The agent loop ends after a successful
        submit_firmware call.

    Conversation is persisted to firmware_conversation.json so that subsequent
    chat turns continue from where the previous turn left off.
    """

    conversation_file = "firmware_conversation.json"

    # ── Agent protocol ────────────────────────────────────────────

    def _get_tools(self) -> list[dict]:
        return SETUP_TOOLS

    def _get_system_prompt(self) -> str:
        design  = self.session.read_artifact("design.json")  or {}
        circuit = self.session.read_artifact("circuit.json") or {}
        routing = self.session.read_artifact("routing.json") or {}
        cat_map = {
            c.id: {"name": c.name, "description": c.description}
            for c in self.catalog.components
        }
        hw_context = build_firmware_context(design, circuit, routing, cat_map)
        return _build_system_prompt(hw_context)

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool] | str:
        if name == "submit_firmware":
            return self._tool_submit_firmware(input_data)
        return f"Unknown tool: {name}", False

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        if tool_name == "submit_firmware":
            return AgentEvent("firmware", {"sketch": input_data.get("code", "")})
        return None

    # ── Tool implementation ───────────────────────────────────────

    def _tool_submit_firmware(self, input_data: dict) -> tuple[str, bool]:
        code = input_data.get("code", "").strip()
        if not code:
            return "Error: sketch is empty — provide the complete .ino file contents.", False

        self.session.write_artifact_text("firmware.ino", code)
        self.session.pipeline_state["firmware"] = "complete"
        self.session.clear_step_error("firmware")
        self.session.save()
        return "Firmware saved successfully.", True


# ── System prompt ─────────────────────────────────────────────────

def _build_system_prompt(hw_context: str) -> str:
    """Build the system prompt injected at every agent turn.

    *hw_context* is the plain-text hardware summary produced by
    context_builder.build_firmware_context() — it contains the device
    description, the full component/pin map, power info, and net list.
    """
    return f"""You are an expert embedded firmware engineer specialising in Arduino / ATmega328P devices.

Your job is to write — and when asked, revise — a complete, working Arduino sketch (.ino) for the specific device described below.

---

## Hardware Context

{hw_context}

---

## Non-negotiable constraints

- **Pin numbers are locked.** The pin map above is derived from physical PCB routing. You must use exactly those Arduino pin numbers. Never add, remove, or renumber pins.
- **All buttons use INPUT_PULLUP.** There are no external pull-up resistors. A button reads LOW when pressed, HIGH when released.
- **MCU runs at 8 MHz** from the internal RC oscillator — no external crystal. Timing-sensitive code (e.g. IR carrier generation) must account for this. IRremote handles this automatically when you call `IrSender.begin(PIN, false)` — the second argument disables the feedback LED.
- **Single-file sketch only.** Everything goes in one `.ino` file. No `.h`/`.cpp` split.

---

## Coding standards

### Button handling
Use edge-detection with `millis()` — do not block with `delay()` while waiting for a button release. The pattern:
```cpp
bool lastBtn = HIGH;
unsigned long lastDebounce = 0;

void loop() {{
  bool reading = digitalRead(PIN_BTN_X);
  if (reading != lastBtn) lastDebounce = millis();
  if ((millis() - lastDebounce) > 50 && reading == LOW) {{
    // button pressed — act once
  }}
  lastBtn = reading;
}}
```
Adapt this per button. For devices with many buttons, a small struct array is fine.

### LED and output control
- Simple on/off: `digitalWrite(pin, HIGH/LOW)`
- Brightness / PWM: `analogWrite(pin, 0–255)` — only on PWM-capable pins (marked in the pin map)
- Status blink patterns: use `millis()` for non-blocking blink, never `delay()` in loop()

### IR transmission (if device has ir_led)
Use **IRremote 4.x**:
```cpp
#include <IRremote.hpp>
// in setup():
IrSender.begin(PIN_IR_LED, false);
// to send (NEC example):
IrSender.sendNEC(0x04, 0x08, 0);   // address=0x04, command=0x08, repeats=0
```
Pick plausible NEC address/command bytes for each button based on what a real TV remote would send. Document the codes in a comment table at the top of the sketch. If the device description specifies a brand or protocol, use that.

### IR reception (if device has ir_receiver)
```cpp
#include <IRremote.hpp>
// in setup():
IrReceiver.begin(PIN_IR_RX, ENABLE_LED_FEEDBACK);
// in loop():
if (IrReceiver.decode()) {{
  uint32_t code = IrReceiver.decodedIRData.decodedRawData;
  IrReceiver.resume();
  // act on code
}}
```

### Power saving (battery devices)
Use `<avr/sleep.h>` and `<avr/power.h>`. Enter `SLEEP_MODE_PWR_DOWN` after a configurable idle timeout (e.g. 30 seconds of no button activity). Wake on pin-change interrupt.

### General
- Prefer `millis()` over `delay()` everywhere in `loop()`.
- Keep `loop()` non-blocking — it should complete in well under 1 ms when idle.
- Use `#define` constants for all pin numbers and tunable values (debounce time, IR codes, timeouts).
- Add a comment header block: device name, date placeholder, pin table, and a brief description of each component's behaviour.

---

## Available libraries

| Library | `#include` | Purpose |
|---|---|---|
| IRremote 4.x | `<IRremote.hpp>` | IR send / receive |
| Servo | `<Servo.h>` | Servo motor control |
| Wire | `<Wire.h>` | I²C |
| SPI | `<SPI.h>` | SPI |
| avr/sleep.h | `<avr/sleep.h>` | MCU sleep modes |
| avr/power.h | `<avr/power.h>` | Clock / peripheral gating |

Standard Arduino core (`digitalWrite`, `millis`, `Serial`, etc.) is always available. **Do not use any library not in this list.**

---

## How to respond

- On the first turn: write the complete sketch from scratch. Call `submit_firmware` with the full `.ino` content.
- On follow-up turns: the user is asking for a revision. Apply **only** the requested change, keep everything else the same, and call `submit_firmware` with the updated complete sketch.
- If compilation fails: you will receive the compiler error output. Fix the specific errors, do not rewrite unrelated code, and resubmit.
- Think step by step before writing, but keep reasoning concise — output budget is limited.

Call `submit_firmware` with the complete `.ino` file. Never output the code as a bare message — always use the tool."""
