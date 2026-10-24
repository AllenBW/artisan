# FZ94 roast log analysis (154 roasts, 2021-08-14 to 2026-10-10)

Source: every `.alog` in `G:\My Drive\Coffee (1)\FZ94\` (read-only). Per-roast and per-event CSVs were written to the session scratchpad during analysis.

How the counts were made:
- Control changes come from the drive readback curves: a change is a settled step of at least 30 Hz (readback ×10 = Hz). This agrees with slider-move counts (589 fan changes vs 597 fan slider bursts).
- Unlabeled events at CHARGE are automatic snapshots of the slider values, not operator actions. This was confirmed in `canvas.py`.

## 1. Cadence, batches, beans
- **154 roasts.** Per year: 27, 26, 25, 30, 32, and 14 so far in 2026. Median 2 roasts per month.
- 132 roast days, 120 of them with a single roast. Median gap between roast days is 12 days.
- **Weight in:** median 1263 g (range 276–2100 g). Batches have grown to about 1400 g in 2025–26.
- **Loss:** median 15.4% (middle half 14.3–17.5%).
- **Weight-out looks wrong in 11 roasts:** 0 g three times, one 11940 g typo, one 54% loss, and 6 others outside 8–25%.
- **Bean names:** 35 different spellings for about 15–20 coffees (Brundi/Burundi, Hatian/Haitian, stray line breaks). Main coffees: El Salvador (~40), Ethiopia Sweet Lily (26), Burundi (22), Dominican (19), India Natural (14).
- **Batch numbers:** #64 is used twice; 19, 29, 31, 63, 98 and 140 are missing.

## 2. Event marks
| Mark | Marked | Notes |
|---|---|---|
| CHARGE | 153 | Automatic (autoCharge is on). Median 12 s of recording before it. |
| TP | 154 | Always computed |
| DRY | 140 | **Manual.** autoDry is off; BT at DRY ranges 134–192 °C. |
| FCs | 126 | 28 missed; 10 of 32 missed in 2025 |
| FCe | 11 | None since 2023 |
| SCs | 30 | |
| SCe | 0 | |
| DROP | 153 | Automatic (autoDrop is on) |
| COOL | 71 | Fading: 24 of 27 in 2021, 2 of 14 in 2026 |

## 3. Control use (CHARGE to DROP)
- **Fan:** median 4 changes per roast (max 10).
- **Drum:** median 3–4 changes; 13 roasts never touch the drum.
- **Heat is never adjusted during a roast.** The Drum Heat Limit is a fixed setting: **200 °C in all 61 roasts since 2024-07-13**. Before that it ranged 190–220 °C.
- Since April 2024 Artisan's heat slider reads 0 at CHARGE while the machine reads 200, so the slider is out of sync.
- **Quantifier ("Q") events are almost all duplicates.** Of 810 in-roast Q events, only 26 lack a matching slider move.
- The Q values are also wrong: fan ×1.2, drum ×0.5. The cause is that the quantifier max (100) doesn't match the slider maxes (120 fan, 50 drum).
- Fan and drum often move together: 41% of fan changes come within 20 s of a drum change.

## 4. Typical control pattern
Fan / drum readback (Hz) at each point:

| Point | 2021–22 | 2024–26 |
|---|---|---|
| CHARGE | 200–300 / 500 | 1200 or 200–300 / 500 |
| TP (~68 s) | 300 / 500 | 200–300 / 500 |
| TP + 60 s | 300 / **200** | 300 / 500 |
| DRY | 500 / 300 | 1200 / 500 |
| FCs | 800 / 350–500 | 600–1200 / 500 |
| DROP | **1200** / 500 | 1200 / 500, or cut to 250–300 |

- Changes follow bean temperature, not the clock. The first drum change comes at a median 120 s (BT 105 °C), the first fan change at 181 s (BT 127 °C).
- **Three styles:**
  - **Progressive (45 roasts, mostly 2021–23):** the fan steps up once; the drum dips after TP and comes back before DROP.
  - **Fan-pulse (96 roasts, now dominant):** the fan goes to 1200 and back down to 300–600 one or more times mid-roast. These roasts are longer and bigger.
  - **Minimal (13 roasts):** fan changes only.

## 5. Roast shape (medians)
| Measure | Median | Trend |
|---|---|---|
| Charge BT / ET | 190 / 183 °C | BT 168 → 216 |
| TP | 68 s at 81 °C | |
| DRY | 5:29 at 149 °C | 312 → 430 s |
| FCs | 9:00 at 188 °C | |
| DROP | 11:20 at 204 °C | 10:06 → 13:13 |
| Development time | 120 s | |
| Development ratio | 18% | |

Rate of rise at DROP has fallen from 8.0 to 1.9 °C/min. Roasts are getting longer and flatter.

## 6. What's used and what isn't
- **Never used:** roasting notes, cupping notes, flavors, title, operator, alarms, color, moisture, ambient, bean size, volume, defect flags, SCe, and the heat slider.
- **Used every time:** bean name and weight in.
- **Used often:** weight out (151 of 154) and background profiles (82%, usually the previous roast of the same bean).

## 7. Friction
- **Slider churn:** of 1774 slider events, about 30% are drag noise or quick corrections. 25 roasts show 65 overshoot-then-reverse moves.
- **Missed or late marks:** FCs missed in 18% of roasts, marked within 20 s of DROP in 3, and apparent double taps in 8.
- **CHARGE looks wrong in 11 roasts**, which also throws off TP.
- **Long recordings:** median 3.2 min after DROP, 24 roasts over 5 min, and two left running for 72 and 52 min.
- **Connection dropouts:** single-sample −1 readings on every Modbus channel in **41 of 50 roasts recorded with 3.1.0**, against 1–3 roasts per older version.

## What this means for a touchscreen-first UI
1. **Fan and drum are the only live controls.** Give each a big control with one-tap presets (fan 200/300/500/600/800/1200, drum 200/250/300/500) plus small up/down nudges, and show the readback next to the target.
2. **Add combined buttons for the paired moves**, e.g. "Fan 1200 + Drum 500".
3. **Show heat as a readout** of the machine's actual value, with editing behind a tap-to-unlock.
4. **Make FCs a large button**, with a reminder based on BT and rate of rise, and allow marking it afterwards. Make DRY one tap. Keep CHARGE and DROP automatic, with a big confirm/undo. Move FCe, SCs, SCe and COOL into an overflow menu. Ignore repeat taps and offer undo.
5. **Big readouts:** BT, BT rate of rise, roast timer, development time/% after FCs, and fan/drum Hz. ET and exhaust can be smaller.
6. **Stop recording automatically after DROP**, and arm automatically before charge.
7. **After the roast:** pick the bean from a list instead of typing it, check weight out for plausibility, and auto-load the last roast of the same bean as the background.
8. **Hide by default:** notes, cupping, flavors, title, alarms, color, moisture, density, ambient.
9. **Fix the duplicate quantifier events**, and add a connection-health indicator.
