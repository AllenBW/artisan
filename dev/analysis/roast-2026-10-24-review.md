# Roast #160 India Natural (2026-10-24): session review

Sources:
- the interaction log, 74 presses
- the saved roast `160 India Natural 26-10-24_2055 1028.alog`
- the screen recording (25:23; 152 review frames in `~/artisan-sessions/session-20261024-204732-review`)
- the app log
- the operator's live notes (`roast-2026-10-24-notes.md`)

There's no Modbus wire trace for this roast: `run-dev.ps1` didn't enable it yet (fixed).

## The roast

| | This roast | Your ~1,200 g roasts (median) | 2025-07-07 India Natural, 1,200 g | 2025-07-13 India Natural, 1,200 g |
|---|---|---|---|---|
| Charge | BT 224 / ET 194 °C | | | |
| Turning point | 1:10, 86 °C | | | |
| DRY END | **5:58**, 160 °C | 5:06 | 6:28 | 4:48 |
| FC START | **8:30**, 188 °C | 8:56 | 8:10 | 8:40 |
| DROP | **10:56**, 199 °C | 11:33 | 11:18 | 11:32 |
| Development | **2:26** (22 %) | 2:12 | 3:08 | 2:52 |
| Weight | 1,201 → 1,028 g, **14.4 %** loss | 15.4 % (all roasts) | | |

Rate of rise: 15.4 °C/min in drying, 11.1 in Maillard, 4.5 in development, 7.5 at first crack.

**The roast landed within about 30 s of your two previous 1,200 g India Naturals at every mark.** The background you loaded (#159, yesterday's test session) and my first comparisons (#155–#158, 1,400–1,800 g) weren't like-for-like; the matched roasts were.

The right element was switched off just after DRY END (about 6:00). That's only in the notes, because elements can't be logged yet.

## How the controls were used
- **Fan and drum changes after 3:30 all went through the touch panel**: 30 touch presses and 44 mouse presses in the session, with **no misses on the panel**.
- Moves in pairs: fan 20 + drum 20 at 1:22, drum 20 + fan 80 at 3:34, **drum 50 + fan 120 at 5:52** (the toss and chaff clear before DRY END), drum 25 + fan 120 at 10:48–10:52 (preparing to drop).
- **Charge state:** fan 120, drum 50.
- **Duplicate tap absorbed:** drum 20 tapped twice at 6:24, one command sent. The fix works.
- **Probable mis-tap:** drum 30 then 50 within a second at 10:26, giving two commands and two events. The presets are 92 px apart, so possibly a slip.
- **Missed taps on empty space:** 6 presses landed on empty frames next to the small bottom-row buttons: three at 3:05 before dragging the drum slider, then at 13:59, 14:17 and 14:56. **All misses were around the old small buttons; none on the panel.**
- **Sliders vanished at ON.** Artisan remembers slider visibility separately for the OFF, ON and recording states. View → Sliders changes the remembered state, so after one toggle while ON (first session), the dev profile hides the sliders every time ON is pressed. You toggled them again at 3:26 and then used only the panel.
- After the drop: COOL END, Roast Properties for the weights (about 20 s), Drum Off and Fan Off with the old bottom buttons (not the panel's Stop), OFF, then Save As.
- The display preset was accepted at startup. That widened the time axis to 15 min, which is why the roast "felt slower" (sampling was 2.0 s throughout).

## Communication
- **0 lost readings on all six channels** over 10:56 of roasting (41 of 50 roasts with 3.1.0 had dropouts). One roast isn't proof; read retries were 1.
- No communication errors and no unconfirmed writes in the log.
- Temperatures come in **whole degrees**, so at 2 s sampling one degree is 30 °C/min of raw rate of rise. That's why the rate-of-rise curve is jumpy. Sampling every 1 s doubles that noise unless smoothing is raised, or the controllers can report tenths of a degree (to check once their model is known).

## What to build next (from this session)
1. **Element buttons:** left, center and right element on/off toggles that log events. Later, read the ET controller's output state over Modbus to capture heat-limit cycling automatically.
2. **Charge preset:** one tap for fan 120 and drum 50 (today's charge state). Optionally wait for a set ET (e.g. 180 °C) and show the countdown.
3. **Background matched to bean and batch size:** default to the latest roast of the same bean within ±10 % of the green weight. Today that would have been the 2025-07-13 India Natural, 1,200 g.
4. **Roast setup before CHARGE:** bean, green weight and background in one place (as in the mock-up), instead of Roast Properties afterwards.
5. **Weigh-out with the cooling container as tare:** enter the scale reading; it shows weight out and loss % with a plausibility check. The container weight was given as 12.46 g, **unit to confirm**.
6. **Cool-down state:** after DROP, show ET counting down to 100 °C and say when the roaster can be switched off.
7. **Slider visibility:** in the dev profile, show sliders in every state (`eventslidersflags 1,1,1`), or hide them for good now that the panel is used. Larger bottom-row buttons, or move Fan/Drum Off onto the panel's Stop.
8. **First-crack rule:** pick one (e.g. "third crack heard") and show it on the FC button.
9. **Sampling:** decide between 1 s with more smoothing or staying at 2 s, after checking whether the controllers can report tenths of a degree.
