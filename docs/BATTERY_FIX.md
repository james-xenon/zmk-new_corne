# Dynamic VDDH battery settling workaround

This repository patches ZMK v0.3.0 `app/src/battery.c` during GitHub Actions.

For Eyelash Corne / nice!nano V2 using `zmk,battery-nrf-vddh`:

- while USB VBUS is present, the last trustworthy battery percentage is preserved;
- after USB is removed, VDDH is sampled normally but the new value is not published immediately;
- relaxation is considered complete after 3 consecutive voltage samples differ by no more than 15 mV;
- with ZMK's default 60-second battery report interval this is normally about 2 minutes at minimum, but it automatically waits longer while voltage continues to move;
- a 15-minute maximum prevents a noisy ADC from holding the old value forever.

Tuning constants are in `patches/patch_battery_vddh.py`:

- `ZMK_VDDH_SETTLE_DELTA_MV`
- `ZMK_VDDH_SETTLE_STABLE_SAMPLES`
- `ZMK_VDDH_SETTLE_MAX_MS`

This improves VDDH-based estimation but does not turn it into a true hardware fuel gauge. While USB is connected, the firmware intentionally does not infer charge progress from VDDH.
