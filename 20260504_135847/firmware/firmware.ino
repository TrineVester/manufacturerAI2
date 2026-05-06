/*
 * ============================================================
 * Device  : Handheld IR Power Remote
 * File    : ir_power_remote.ino
 * Date    : [DATE]
 * ============================================================
 *
 * Description:
 *   A single-button handheld IR remote.  Each press of the
 *   tactile power button transmits a NEC "power toggle"
 *   command on a 38 kHz carrier via the IR LED.
 *
 *   After IDLE_TIMEOUT_MS of inactivity the MCU enters
 *   PWR_DOWN sleep to preserve battery life.  A button press
 *   wakes the MCU via a pin-change interrupt on pin 13 (PB5 /
 *   PCINT5); the press is then debounced normally and the IR
 *   command is sent.
 *
 * Hardware:
 *   MCU  : ATmega328P @ 8 MHz internal RC oscillator
 *   Power: 2× AAA batteries
 *
 * Pin Map:
 *   Pin  9  – IR LED anode (through current-limit resistor)
 *             PWM output, 38 kHz NEC carrier (IRremote 4.x)
 *   Pin 13  – Tactile power button (INPUT_PULLUP, active LOW)
 *
 * IR Code Table (NEC protocol, 32-bit LSB-first framing):
 *   Button | Address | Command | Action
 *   -------|---------|---------|----------------------------
 *   Power  |  0x04   |  0x08   | Toggle target-device power
 *
 * ============================================================
 */

#include <IRremote.hpp>
#include <avr/sleep.h>
#include <avr/power.h>

// ── Pin Definitions ──────────────────────────────────────────
#define PIN_BTN_POWER   13      // Tactile button  (INPUT_PULLUP, active LOW)
#define PIN_IR_LED       9      // IR LED anode    (PWM, 38 kHz carrier)

// ── NEC IR Codes ─────────────────────────────────────────────
#define IR_NEC_ADDR     0x04    // NEC device address byte
#define IR_NEC_CMD      0x08    // NEC power-toggle command byte

// ── Tunable Constants ────────────────────────────────────────
#define DEBOUNCE_MS     50UL        // Stable-low required before acting  (ms)
#define IDLE_TIMEOUT_MS 30000UL     // Enter sleep after this idle period (ms)

// ── Button State ─────────────────────────────────────────────
static bool          lastBtnState   = HIGH; // Last stable reading (released)
static unsigned long lastDebounceMs = 0;    // Time of last state change
static bool          btnActed       = false;// One-shot guard per press

// ── Activity Tracking ────────────────────────────────────────
static unsigned long lastActivityMs = 0;    // Time of last button event

// ── Pin-Change ISR (PCINT0 group = pins 8-13 / PB0-PB5) ─────
// PCINT5 = PB5 = Arduino pin 13
// The ISR body is intentionally empty; its sole purpose is to
// wake the CPU from PWR_DOWN sleep.
ISR(PCINT0_vect)
{
    // Wake-up only – no action required here
}

// ── Sleep Helper ─────────────────────────────────────────────
static void goToSleep(void)
{
    // Make sure the IR LED is driven LOW before sleeping
    digitalWrite(PIN_IR_LED, LOW);

    // ── Arm pin-change interrupt on PB5 (Arduino pin 13) ──
    PCMSK0 |= (1 << PCINT5);   // Unmask PCINT5
    PCIFR  |= (1 << PCIF0);    // Clear any stale pending flag
    PCICR  |= (1 << PCIE0);    // Enable PCIE0 interrupt group

    // Disable ADC to minimise sleep current
    ADCSRA &= ~(1 << ADEN);

    set_sleep_mode(SLEEP_MODE_PWR_DOWN);
    sleep_enable();
    sleep_cpu();                // ←─ CPU halts here until PCINT fires ─→

    // ── Execution resumes here after pin-change wakes MCU ──
    sleep_disable();

    // Re-enable ADC
    ADCSRA |= (1 << ADEN);

    // Disable PCIE0 – we only need it to initiate wakeup
    PCICR &= ~(1 << PCIE0);

    // Reset idle timer so we do not immediately sleep again
    lastActivityMs = millis();
}

// ── Setup ────────────────────────────────────────────────────
void setup(void)
{
    pinMode(PIN_BTN_POWER, INPUT_PULLUP);

    // Initialise IR sender; false = disable built-in feedback LED
    IrSender.begin(PIN_IR_LED, false);

    lastActivityMs = millis();
    lastBtnState   = digitalRead(PIN_BTN_POWER);
}

// ── Main Loop ────────────────────────────────────────────────
void loop(void)
{
    // ── Non-blocking button debounce ─────────────────────────
    bool reading = digitalRead(PIN_BTN_POWER);

    if (reading != lastBtnState) {
        // Any state change restarts the debounce window
        lastDebounceMs = millis();
    }

    if ((millis() - lastDebounceMs) > DEBOUNCE_MS) {
        // Signal has been stable for the full debounce window

        if (reading == LOW && !btnActed) {
            // ── Confirmed button press ────────────────────────
            // Send NEC power-toggle command (0 repeat frames)
            IrSender.sendNEC(IR_NEC_ADDR, IR_NEC_CMD, 0);

            lastActivityMs = millis();  // Reset idle timeout
            btnActed = true;            // Prevent re-firing while held
        }

        if (reading == HIGH) {
            // Button released – re-arm for the next press
            btnActed = false;
        }
    }

    lastBtnState = reading;

    // ── Idle-timeout sleep check ──────────────────────────────
    if ((millis() - lastActivityMs) >= IDLE_TIMEOUT_MS) {
        goToSleep();
        // After returning from goToSleep(), lastActivityMs has
        // been refreshed; the button press that woke us will be
        // processed normally on the next loop iteration.
    }
}