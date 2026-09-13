# Hardware compatibility

Read this before anything else. It decides whether this software can fly your
aircraft at all.

## Summary

| Aircraft | Can this software fly it? | Why |
|---|---|---|
| PX4 / ArduPilot (Pixhawk, Cube, Holybro…) | **Yes, today** | MAVSDK backend drives it directly |
| DJI Mini 4 Pro, Mini 3, Mini 3 Pro | Not yet — needs a port | MSDK v5 supported, but MSDK is Android/iOS only |
| DJI Matrice 300/350, M30, Mavic 3 Enterprise | Not yet — needs a port | MSDK v5 / PSDK supported |
| **DJI Mini 4K** | **No** | No SDK of any kind |
| DJI Air 3, Mavic 3 Classic, Mini 2, Mini SE | **No** | Not on the MSDK v5 supported list |

## The DJI Mini 4K

The Mini 4K cannot run custom flight software, and no amount of engineering on
this end changes that. DJI's own product SDK compatibility table lists it as
**SDK compatibility: No**:

- **No Mobile SDK v5.** It is not on the supported-product list.
- **No Mobile SDK v4.** Development on v4 ended before the Mini 4K shipped.
- **No Payload SDK.** There is no E-Port or SkyPort on the airframe.
- **No Waypoint mode**, even inside the DJI Fly app.

Third-party apps do not provide a way around this. Litchi and similar tools fly
DJI aircraft *through* the Mobile SDK — the same SDK the Mini 4K does not expose.
If the SDK is closed, everything built on it is closed too.

The Mini 4K is essentially a Mini 2 SE with a better camera. That is a fine
aircraft to fly manually; it is simply not a programmable one.

## What to do instead

**If you want to fly this software soon:** a PX4 or ArduPilot build works with
no porting at all. The MAVSDK backend drives it directly, and you can test the
whole thing in SITL before going near a field.

**If you want to stay with DJI:** a Mini 4 Pro, Mini 3, or Mini 3 Pro is the
cheapest supported option. Note two things:

1. MSDK v5 is **Android/iOS only** (Kotlin/Swift). The safety engine in this
   repository would need porting to Kotlin — the rule table, thresholds and
   arbitration logic transfer directly, but the code does not.
2. Waypoint support on consumer models is documented as **"limited"** compared
   to the enterprise line. Confirm the specific capabilities you need against
   `developer.dji.com` before buying.

## Why the work still transfers

Everything that makes this system worth having is airframe-independent: the
nineteen safety rules, the severity arbitration, the return-energy estimator,
the predictive geofence, the terrain handling, the preflight gate. Those are the
parts that took the thought, and they are validated in the simulator rather than
in the air.

A backend is roughly 300 lines. The safety engine it protects is the asset.
