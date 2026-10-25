# Roast 2026-10-24: operator notes (live)

Session: dev build (Artisan-Dev), ON at 20:46:20, screen recording from 20:47:32
(`~/artisan-sessions/session-20261024-204732.mkv`). No Modbus wire trace this session (`run-dev.ps1` didn't enable it yet; fixed for next time).

## 20:52 (preheat)
- Waiting for ET to reach at least 180 °C. Not a sequential roast day, so the drum isn't heat-soaked.
- Unsure whether 180 is the right charge point, or whether to let ET run to 200 (the heat limit), let the heat cut out, and charge when it comes back on.
- **Fan** is used intermittently: added when there's a lot of chaff in the container, or to bring ET down.
- **Drum** speed sets the pace: faster speeds the roast up, slower keeps it slower. Faster also shakes chaff loose so the fan pulls it off.
- The machine heats with electric elements, like an oven: conduction, convection and infrared, in a perforated drum. There are three elements (**left, center, right**). They're simply on or off, and there's little control over them.
- Element on/off isn't captured anywhere today, and probably isn't on Modbus. **Wanted: buttons to log when each element is switched on and off.**

## 20:55 (charging)
- ET reached 180 °C, charging now (the operator calls loading the beans "drop"; it's Artisan's CHARGE).
- Charging options weighed: a little at a time or all at once; maybe fan fully on and drum at 100 % during charging. Decided by feel today.
- **Goal stated:** make these decisions not about feel. Wanted: a standard, repeatable charge procedure (one-tap charge preset for fan and drum, and a consistent charge ET), then compare outcomes across roasts.

## 21:00 (drying)
- Waiting for the beans to go green → pale → brown; using colour and smell (the sweet Maillard smell) as cues.
- Chaff building up: plan to clear it by tumbling more (faster drum) and more fan.

## 21:01 (end of drying → Maillard)
- Believes drying is done; a fast toss (faster drum) to check no green beans remain.
- Next concern: first crack. Goal: avoid an underdeveloped roast by drawing the roast out so first crack comes late, around 9–14 min (ideally ~14).
- The operator plans to evaluate and tweak this approach afterwards.

## 21:03:19 (Maillard)
- DRY END tapped at 21:01:42, roast time **5:59** (CHARGE auto-detected at about 20:55:43).
- **Right element switched OFF** just after DRY END, because the temperature was high. (Element switches aren't logged in Artisan yet, so this note is the only record.)
- Worry: first crack too early (~8 min), which the operator considers underdeveloped; aiming for ~14 min.
- Operator's reasoning: an underdeveloped roast needs a much finer grind to extract the same.

## 21:04:53 (first crack)
- **FC START tapped at 21:04:15, roast time 8:32** (history at ~1,200 g: median 8:56; India Natural at 1,200 g: 8:10, 8:40).
- Batch is **1,200 g**, not 1,800 g: the loaded background (#158, 1,800 g) is the wrong reference. Set the green weight in Roast Properties afterwards.
- Unsure when exactly to mark first crack: the very first crack, or once cracks are consistent ("rolling")? It was rolling, definitely first crack and not second-crack sizzle. **Needs a consistent rule** (see the reply in the session).

## 21:11:21 (after the drop)
- **DROP at 21:06:44, roast time 11:01.** Phases: drying 5:59 (54 %), Maillard 2:33 (23 %), development 2:29 (22.5 %).
- Weighed in the cooling container (tare subtracted by hand; container weight given as 12.46 g, unit to be confirmed).
- **Weight loss about 14 %** (usual 14–18 %, median 15.4 %).
- Shutdown routine: wait until ET is below 100 °C, then switch the roaster off. Recording kept running through cool-down at the operator's request.
