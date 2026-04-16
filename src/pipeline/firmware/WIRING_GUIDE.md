# Universal IR Remote - Complete Wiring Guide

## Parts Required

| Component                     | Quantity  | Notes                             |
|-------------------------------|-----------|-----------------------------------|
| Elegoo Uno R3 / Arduino Uno   | 1         | Main controller                   |
| Breadboard                    | 1         | Full size (a-j, 1-63)             |
| IR LED (940nm)                | 1         | 5mm, clear or blue tint           |
| 100-150Ω resistor             | 1         | For IR LED                        |
| 220-330Ω resistor             | 1         | For status LED (optional)         |
| Colored LED                   | 1         | Any color, for status (optional)  |
| Tactile push buttons          | 1-9       | 6mm x 6mm typical                 |
| Jumper wires                  | ~15       | Male-to-male                      |

---

## Pin Reference Table

| Arduino Pin   | Function      | Required? | Wire Color Suggestion |
|---------------|---------------|-----------|-----------------------|
| **3**         | IR LED output | **YES**   | Red                   |
| **2**         | Power button  | **YES**   | Orange                |
| **4**         | Volume Up     | Optional  | Yellow                |
| **5**         | Volume Down   | Optional  | Green                 |
| **6**         | Channel 1     | Optional  | Blue                  |
| **7**         | Channel 2     | Optional  | Purple                |
| **8**         | Channel 3     | Optional  | Gray                  |
| **9**         | Channel 4     | Optional  | White                 |
| **10**        | Channel 5     | Optional  | Brown                 |
| **12**        | Brand Select  | Optional  | Pink                  |
| **13**        | Status LED    | Optional  | Black                 |
| **GND**       | Ground        | **YES**   | Black                 |

---

## Detailed Wiring Instructions

### Step 1: IR LED (Required)

The IR LED is needed to send signals to the TV.

```
Arduino Pin 3 ──────┬──[100-150Ω]──┬── IR LED Anode (long leg)
                    │              │
                    │              └── IR LED Cathode (short leg) ── GND
```

**Breadboard layout:**

| Row   | Column a-e                        | Column f-j    | Notes             |
|-------|-----------------------------------|---------------|-------------------|
| 5     | Wire from Pin 3                   |               | Red wire          |
| 5-8   | Resistor leg 1                    |               | Spans rows 5-8    |
| 8     | Resistor leg 2 + IR LED long leg  |               |                   |
| 9     | IR LED short leg                  |               |                   |
| 9     | Wire to GND                       |               | Black wire        |

**Step-by-step:**
1. Insert 100-150Ω resistor from **row 5** to **row 8** (column a)
2. Insert IR LED: long leg in **row 8** (column b), short leg in **row 9** (column b)
3. Connect jumper wire from **Arduino Pin 3** to **row 5** (column c)
4. Connect jumper wire from **row 9** (column c) to **Arduino GND**

**Resistor colors for 100Ω:** Brown - Black - Brown - Gold  
**Resistor colors for 150Ω:** Brown - Green - Brown - Gold

---

### Step 2: Power Button (Required)

This is the main button - quick press for power, hold 5 seconds for brand scan.

```
Arduino Pin 2 ──── Button ──── GND
```

**Breadboard layout:**

| Row   | Column a-e        | Column f-j    | Notes             |
|-------|-------------------|---------------|-------------------|
| 15    | Wire from Pin 2   |               | Orange wire       |
| 15-17 | Button leg 1      | Button leg 2  | Button spans gap  |
| 17    | Wire to GND       |               | Black wire        |

**Step-by-step:**
1. Insert button so it **spans the center gap** (legs in e15, f15, e17, f17)
2. Connect jumper wire from **Arduino Pin 2** to **row 15** (column a)
3. Connect jumper wire from **row 17** (column a) to **Arduino GND**

---

### Step 3: Status LED (Optional but Recommended)

Shows when commands are sent and indicates scan mode.

```
Arduino Pin 13 ──[220-330Ω]── LED Anode (long leg)
                              LED Cathode (short leg) ── GND
```

**Breadboard layout:**

| Row   | Column a-e            | Column f-j | Notes        |
|-------|-----------------------|------------|--------------|
| 55    | Wire from Pin 13      |            | White wire   |
| 55-58 | Resistor              |            |              |
| 58    | LED long leg          |            |              |
| 59    | LED short leg → GND   |            |              |

**Step-by-step:**
1. Insert 220-330Ω resistor from **row 55** to **row 58** (column a)
2. Insert colored LED: long leg in **row 58** (column b), short leg in **row 59** (column b)
3. Connect jumper from **Arduino Pin 13** to **row 55** (column c)
4. Connect jumper from **row 59** (column c) to **Arduino GND**

**Resistor colors for 220Ω:** Red - Red - Brown - Gold  
**Resistor colors for 330Ω:** Orange - Orange - Brown - Gold

---

### Step 4: Additional Buttons (Optional)

Add buttons as needed. All buttons wire the same way: **Pin → Button → GND**

#### Volume Up (Pin 4)
| Row   | Connection        |
|-------|-------------------|
| 20    | Wire from Pin 4   |
| 20-22 | Button spans gap  |
| 22    | Wire to GND       |

#### Volume Down (Pin 5)
| Row   | Connection        |
|-------|-------------------|
| 25    | Wire from Pin 5   |
| 25-27 | Button spans gap  |
| 27    | Wire to GND       |

#### Channel 1 (Pin 6)
| Row   | Connection        |
|-------|-------------------|
| 30    | Wire from Pin 6   |
| 30-32 | Button spans gap  |
| 32    | Wire to GND       |

#### Channel 2 (Pin 7)
| Row   | Connection        |
|-------|-------------------|
| 35    | Wire from Pin 7   |
| 35-37 | Button spans gap  |
| 37    | Wire to GND       |

#### Channel 3 (Pin 8)
| Row   | Connection        |
|-------|-------------------|
| 40    | Wire from Pin 8   |
| 40-42 | Button spans gap  |
| 42    | Wire to GND       |

#### Channel 4 (Pin 9)
| Row   | Connection        |
|-------|-------------------|
| 45    | Wire from Pin 9   |
| 45-47 | Button spans gap  |
| 47    | Wire to GND       |

#### Channel 5 (Pin 10)
| Row   | Connection        |
|-------|-------------------|
| 50    | Wire from Pin 10  |
| 50-52 | Button spans gap  |
| 52    | Wire to GND       |

#### Brand Select (Pin 12)
| Row   | Connection        |
|-------|-------------------|
| 60    | Wire from Pin 12  |
| 60-62 | Button spans gap  |
| 62    | Wire to GND       |

---

## Complete Breadboard Diagram

```
         BREADBOARD (a-j, rows 1-63)
    ─────────────────────────────────────
    a   b   c   d   e │ f   g   h   i   j
    ─────────────────────────────────────
 5  [RESISTOR 100Ω]   │                     ← Pin 3 (IR)
 6  [RESISTOR    ]    │
 7  [RESISTOR    ]    │
 8  [RES][LED+]       │                     ← IR LED anode
 9       [LED-]───────│─────────────────    → GND
    ─────────────────────────────────────
15  [PIN2]       [BTN │ BTN]                ← Power button
16               [BTN │ BTN]
17  [GND]        [BTN │ BTN]                → GND
    ─────────────────────────────────────
20  [PIN4]       [BTN │ BTN]                ← Vol+ button
21               [BTN │ BTN]
22  [GND]        [BTN │ BTN]                → GND
    ─────────────────────────────────────
25  [PIN5]       [BTN │ BTN]                ← Vol- button
26               [BTN │ BTN]
27  [GND]        [BTN │ BTN]                → GND
    ─────────────────────────────────────
    ... (add more buttons as needed) ...
    ─────────────────────────────────────
55  [RESISTOR 220Ω]   │                     ← Pin 13 (Status)
56  [RESISTOR    ]    │
57  [RESISTOR    ]    │
58  [RES][LED+]       │                     ← Status LED anode
59       [LED-]───────│─────────────────    → GND
    ─────────────────────────────────────
```

---

## GND Wiring Strategy

You have multiple components needing GND. Use the **power rails** on the sides of the breadboard:

1. Connect **Arduino GND** to the **blue (-)** rail on one side
2. Connect all component GNDs to this same rail
3. This keeps wiring clean and organized

```
Arduino GND ────────────────────────────────
                │
    ┌───────────┴───────────────────────┐
    │           BLUE RAIL (-)           │
    └─┬─────┬─────┬─────┬─────┬─────┬───┘
      │     │     │     │     │     │
    IR LED  BTN1  BTN2  BTN3  ...  Status LED
```

---

## Minimum Working Setup

Need just these 3 components to test the remote:

1. **IR LED** on Pin 3 (with 100-150Ω resistor)
2. **Power button** on Pin 2
3. **Wires to GND**

That's it! The Power button can:
- Quick press = send power signal
- Hold 5 seconds = scan through TV brands

---

## Before Uploading

1. **Install IRremote library:**
   - Arduino IDE → Tools → Manage Libraries
   - Search "IRremote"
   - Install "IRremote by shirriff, z3t0, ArminJo"

2. **Select board:** Tools → Board → Arduino Uno

3. **Select port:** Tools → Port → COM7 (Arduino Uno)

4. **Upload:** Click → or Ctrl+U

---

## Testing

1. Open **Serial Monitor** (Ctrl+Shift+M, 9600 baud)
2. Press **Power button** quickly - should see "Sending power command"
3. Hold **Power button** 5 seconds - enters scan mode
4. Point at TV and press buttons to test

---

## Troubleshooting

| Problem                       | Solution                                                  |
|-------------------------------|-----------------------------------------------------------|
| IR LED doesn't light up       | IR is invisible! Use phone camera to see it glow purple   |
| Button not responding         | Check button spans center gap correctly                   |
| TV doesn't respond            | Try scan mode to find correct brand                       |
| "IRremote" error on compile   | Install the IRremote library                              |
| Status LED always on/off      | Check polarity (long leg = positive)                      |
