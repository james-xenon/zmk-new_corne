/*
 * Copyright (c) 2020 The ZMK Contributors
 *
 * SPDX-License-Identifier: MIT
 *
 * Eyelash Corne smooth RGB36 extension:
 * - existing effect IDs 0..15 are preserved for saved-settings compatibility
 * - effects 16..35 add new ambient, motion, particle, and themed animations
 * - all rendered frames pass through a common temporal smoothing layer
 * - RGB_EFF/RGB_EFR use a separate logical cycle order without renumbering IDs
 * - rgb_underglow_state layout is intentionally unchanged for settings compatibility
 */

#include <zephyr/device.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>

#include <math.h>
#include <stdlib.h>
#include <stdint.h>

#include <zephyr/logging/log.h>

#include <zephyr/drivers/led_strip.h>
#include <drivers/ext_power.h>

#include <zmk/rgb_underglow.h>
#include <zmk/activity.h>
#include <zmk/usb.h>
#include <zmk/event_manager.h>
#include <zmk/events/activity_state_changed.h>
#include <zmk/events/usb_conn_state_changed.h>
#include <zmk/workqueue.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

#if !DT_HAS_CHOSEN(zmk_underglow)

#error "A zmk,underglow chosen node must be declared"

#endif

#define STRIP_CHOSEN DT_CHOSEN(zmk_underglow)
#define STRIP_NUM_PIXELS DT_PROP(STRIP_CHOSEN, chain_length)
#define HUE_MAX 360
#define SAT_MAX 100
#define BRT_MAX 100

/* Eyelash Corne wiring used by this configuration:
 * 21 physical LEDs = 3 rows x 7 logical column positions.
 * Pixel indices 0/7/14 are logical column 0, 1/8/15 are column 1, etc.
 * The original effects 0..3 intentionally do NOT use this mapping.
 */
#define EYELASH_LOGICAL_COLUMNS 7

BUILD_ASSERT(CONFIG_ZMK_RGB_UNDERGLOW_BRT_MIN <= CONFIG_ZMK_RGB_UNDERGLOW_BRT_MAX,
             "ERROR: RGB underglow maximum brightness is less than minimum brightness");
BUILD_ASSERT((STRIP_NUM_PIXELS % EYELASH_LOGICAL_COLUMNS) == 0,
             "RGB16 effects expect the Eyelash LED count to be divisible by 7");

enum rgb_underglow_effect {
    /* Existing IDs 0..15 stay stable for saved-settings compatibility. */
    UNDERGLOW_EFFECT_SOLID = 0,
    UNDERGLOW_EFFECT_BREATHE,
    UNDERGLOW_EFFECT_SPECTRUM,
    UNDERGLOW_EFFECT_SWIRL,
    UNDERGLOW_EFFECT_COLUMN_RAINBOW,
    UNDERGLOW_EFFECT_RAINBOW_WAVE,
    UNDERGLOW_EFFECT_SCANNER,
    UNDERGLOW_EFFECT_CENTER_OUT,
    UNDERGLOW_EFFECT_METEOR,
    UNDERGLOW_EFFECT_PULSE_WAVE,
    UNDERGLOW_EFFECT_ALTERNATING,
    UNDERGLOW_EFFECT_MOVING_GRADIENT,
    UNDERGLOW_EFFECT_AURORA,
    UNDERGLOW_EFFECT_SPARKLE,
    UNDERGLOW_EFFECT_FIRE,
    UNDERGLOW_EFFECT_MATRIX,

    /* New smooth Eyelash effects. */
    UNDERGLOW_EFFECT_PASTEL_FLOW,
    UNDERGLOW_EFFECT_OCEAN,
    UNDERGLOW_EFFECT_SUNSET,
    UNDERGLOW_EFFECT_LAVA,
    UNDERGLOW_EFFECT_FOREST,
    UNDERGLOW_EFFECT_PLASMA,
    UNDERGLOW_EFFECT_COMET,
    UNDERGLOW_EFFECT_DUAL_COMET,
    UNDERGLOW_EFFECT_DUAL_SCANNER,
    UNDERGLOW_EFFECT_EDGE_IN,
    UNDERGLOW_EFFECT_CENTER_GLOW,
    UNDERGLOW_EFFECT_RIPPLE,
    UNDERGLOW_EFFECT_TWINKLE,
    UNDERGLOW_EFFECT_FIREFLIES,
    UNDERGLOW_EFFECT_CANDLE,
    UNDERGLOW_EFFECT_EMBERS,
    UNDERGLOW_EFFECT_SOFT_RAIN,
    UNDERGLOW_EFFECT_GALAXY,
    UNDERGLOW_EFFECT_SHIMMER,
    UNDERGLOW_EFFECT_NORTHERN_LIGHTS,

    UNDERGLOW_EFFECT_NUMBER
};

/*
 * Cycling order is intentionally independent from the numeric IDs above.
 * This keeps old saved effect IDs valid while making RGB_EFF/RGB_EFR feel
 * logically grouped: base/color -> ambient -> motion -> particles/themes.
 */
static const uint8_t eyelash_effect_cycle_order[] = {
    UNDERGLOW_EFFECT_SOLID,
    UNDERGLOW_EFFECT_BREATHE,
    UNDERGLOW_EFFECT_SPECTRUM,
    UNDERGLOW_EFFECT_SWIRL,
    UNDERGLOW_EFFECT_COLUMN_RAINBOW,
    UNDERGLOW_EFFECT_RAINBOW_WAVE,
    UNDERGLOW_EFFECT_MOVING_GRADIENT,
    UNDERGLOW_EFFECT_PASTEL_FLOW,
    UNDERGLOW_EFFECT_AURORA,
    UNDERGLOW_EFFECT_OCEAN,
    UNDERGLOW_EFFECT_SUNSET,
    UNDERGLOW_EFFECT_FOREST,
    UNDERGLOW_EFFECT_LAVA,
    UNDERGLOW_EFFECT_PLASMA,
    UNDERGLOW_EFFECT_PULSE_WAVE,
    UNDERGLOW_EFFECT_SHIMMER,

    UNDERGLOW_EFFECT_SCANNER,
    UNDERGLOW_EFFECT_DUAL_SCANNER,
    UNDERGLOW_EFFECT_CENTER_OUT,
    UNDERGLOW_EFFECT_CENTER_GLOW,
    UNDERGLOW_EFFECT_EDGE_IN,
    UNDERGLOW_EFFECT_RIPPLE,
    UNDERGLOW_EFFECT_METEOR,
    UNDERGLOW_EFFECT_COMET,
    UNDERGLOW_EFFECT_DUAL_COMET,
    UNDERGLOW_EFFECT_ALTERNATING,
    UNDERGLOW_EFFECT_MATRIX,
    UNDERGLOW_EFFECT_SOFT_RAIN,

    UNDERGLOW_EFFECT_SPARKLE,
    UNDERGLOW_EFFECT_TWINKLE,
    UNDERGLOW_EFFECT_FIREFLIES,
    UNDERGLOW_EFFECT_FIRE,
    UNDERGLOW_EFFECT_CANDLE,
    UNDERGLOW_EFFECT_EMBERS,
    UNDERGLOW_EFFECT_GALAXY,
    UNDERGLOW_EFFECT_NORTHERN_LIGHTS,
};

struct rgb_underglow_state {
    struct zmk_led_hsb color;
    uint8_t animation_speed;
    uint8_t current_effect;
    uint16_t animation_step;
    bool on;
};

static const struct device *led_strip;

static struct led_rgb pixels[STRIP_NUM_PIXELS];

/*
 * The effect functions render into pixels[] (target frame). The strip itself
 * is driven from smoothed_pixels[]. This single temporal low-pass layer makes
 * every effect transition gradual, including effect changes and deterministic
 * random effects, without changing the persisted rgb_underglow_state layout.
 *
 * 4 = softer/slower transitions, 3 = faster, 5 = very soft.
 */
#define EYELASH_RGB_SMOOTH_DIVISOR 4
static struct led_rgb smoothed_pixels[STRIP_NUM_PIXELS];
static bool smoothed_pixels_initialized;


/* IMPORTANT: do not add fields here. This exact state layout is stored in settings. */
static struct rgb_underglow_state state;
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_EXT_POWER)
static const struct device *const ext_power = DEVICE_DT_GET(DT_INST(0, zmk_ext_power_generic));
#endif

static struct zmk_led_hsb hsb_scale_min_max(struct zmk_led_hsb hsb) {
    hsb.b = CONFIG_ZMK_RGB_UNDERGLOW_BRT_MIN +
            (CONFIG_ZMK_RGB_UNDERGLOW_BRT_MAX - CONFIG_ZMK_RGB_UNDERGLOW_BRT_MIN) * hsb.b / BRT_MAX;
    return hsb;
}
static struct zmk_led_hsb hsb_scale_zero_max(struct zmk_led_hsb hsb) {
    hsb.b = hsb.b * CONFIG_ZMK_RGB_UNDERGLOW_BRT_MAX / BRT_MAX;
    return hsb;
}

static struct led_rgb hsb_to_rgb(struct zmk_led_hsb hsb) {
    float r = 0, g = 0, b = 0;

    uint8_t i = hsb.h / 60;
    float v = hsb.b / ((float)BRT_MAX);
    float s = hsb.s / ((float)SAT_MAX);
    float f = hsb.h / ((float)HUE_MAX) * 6 - i;
    float p = v * (1 - s);
    float q = v * (1 - f * s);
    float t = v * (1 - (1 - f) * s);
    switch (i % 6) {
    case 0:
        r = v;
        g = t;
        b = p;
        break;
    case 1:
        r = q;
        g = v;
        b = p;
        break;
    case 2:
        r = p;
        g = v;
        b = t;
        break;
    case 3:
        r = p;
        g = q;
        b = v;
        break;
    case 4:
        r = t;
        g = p;
        b = v;
        break;
    case 5:
        r = v;
        g = p;
        b = q;
        break;
    }
    struct led_rgb rgb = {r : r * 255, g : g * 255, b : b * 255};

    return rgb;
}

/* --------------------------------------------------------------------------
 * Eyelash RGB helpers and globally-smoothed renderer.
 * -------------------------------------------------------------------------- */

static inline uint8_t eyelash_logical_column(int pixel_index) {
    return (uint8_t)(pixel_index % EYELASH_LOGICAL_COLUMNS);
}

static inline uint8_t eyelash_logical_row(int pixel_index) {
    return (uint8_t)(pixel_index / EYELASH_LOGICAL_COLUMNS);
}

static uint16_t eyelash_wrap_hue(int hue) {
    hue %= HUE_MAX;
    if (hue < 0) {
        hue += HUE_MAX;
    }
    return (uint16_t)hue;
}

static uint8_t eyelash_triangle_percent(uint16_t phase, uint16_t period) {
    if (period < 2) {
        return 100;
    }

    phase %= period;
    const uint16_t half = period / 2U;

    if (phase <= half) {
        return (uint8_t)((phase * 100U) / MAX(half, 1U));
    }

    return (uint8_t)(((period - phase) * 100U) / MAX(period - half, 1U));
}

static uint8_t eyelash_lerp_u8(uint8_t a, uint8_t b, uint8_t pct) {
    int value = (int)a + (((int)b - (int)a) * (int)pct) / 100;
    return (uint8_t)CLAMP(value, 0, 255);
}

static uint8_t eyelash_peak_percent(uint16_t distance, uint16_t radius, uint8_t floor_pct) {
    if (radius == 0 || distance >= radius) {
        return floor_pct;
    }

    return floor_pct +
           (uint8_t)(((uint32_t)(100U - floor_pct) * (radius - distance)) / radius);
}

/* Small deterministic hash. Random-looking effects interpolate between
 * deterministic keyframes, so they are repeatable and never hard-flicker. */
static uint32_t eyelash_hash32(uint32_t x) {
    x ^= x >> 16;
    x *= 0x7feb352dU;
    x ^= x >> 15;
    x *= 0x846ca68bU;
    x ^= x >> 16;
    return x;
}

/* brightness_percent is relative to the user's current RGB brightness. */
static struct led_rgb eyelash_effect_rgb(int hue, uint8_t saturation,
                                         uint8_t brightness_percent) {
    struct zmk_led_hsb hsb = state.color;

    saturation = MIN(saturation, SAT_MAX);
    brightness_percent = MIN(brightness_percent, 100U);

    hsb.h = eyelash_wrap_hue(hue);
    hsb.s = saturation;
    hsb.b = (uint8_t)(((uint16_t)state.color.b * brightness_percent) / 100U);

    return hsb_to_rgb(hsb_scale_zero_max(hsb));
}

static uint8_t eyelash_smooth_channel(uint8_t current, uint8_t target) {
    if (current == target) {
        return current;
    }

    int16_t delta = (int16_t)target - (int16_t)current;
    int16_t step = delta / EYELASH_RGB_SMOOTH_DIVISOR;

    if (step == 0) {
        step = (delta > 0) ? 1 : -1;
    }

    return (uint8_t)((int16_t)current + step);
}

static int eyelash_flush_smoothed_frame(void) {
    if (!smoothed_pixels_initialized) {
        for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
            smoothed_pixels[i] = (struct led_rgb){r : 0, g : 0, b : 0};
        }
        smoothed_pixels_initialized = true;
    }

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        smoothed_pixels[i].r = eyelash_smooth_channel(smoothed_pixels[i].r, pixels[i].r);
        smoothed_pixels[i].g = eyelash_smooth_channel(smoothed_pixels[i].g, pixels[i].g);
        smoothed_pixels[i].b = eyelash_smooth_channel(smoothed_pixels[i].b, pixels[i].b);
    }

    return led_strip_update_rgb(led_strip, smoothed_pixels, STRIP_NUM_PIXELS);
}

static uint8_t eyelash_interpolated_random_level(uint32_t seed, uint16_t step,
                                                  uint16_t keyframe_len,
                                                  uint8_t min_pct,
                                                  uint8_t max_pct) {
    const uint32_t frame = step / keyframe_len;
    const uint8_t frac = (uint8_t)(((step % keyframe_len) * 100U) / keyframe_len);

    const uint32_t r0 = eyelash_hash32(seed + frame * 0x9e3779b9U);
    const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x9e3779b9U);

    const uint8_t span = max_pct - min_pct;
    const uint8_t a = min_pct + (uint8_t)(r0 % (span + 1U));
    const uint8_t b = min_pct + (uint8_t)(r1 % (span + 1U));

    return eyelash_lerp_u8(a, b, frac);
}

/* --------------------------------------------------------------------------
 * Existing effects 0..15. IDs are retained for settings compatibility.
 * Their visual updates are now passed through the common smooth renderer.
 * -------------------------------------------------------------------------- */

/* 0: Solid user color. */
static void zmk_rgb_underglow_effect_solid(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        pixels[i] = hsb_to_rgb(hsb_scale_min_max(state.color));
    }
}

/* 1: Whole-strip breathing. */
static void zmk_rgb_underglow_effect_breathe(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        struct zmk_led_hsb hsb = state.color;
        hsb.b = abs(state.animation_step - 1200) / 12;
        pixels[i] = hsb_to_rgb(hsb_scale_zero_max(hsb));
    }

    state.animation_step += state.animation_speed * 10;

    if (state.animation_step > 2400) {
        state.animation_step = 0;
    }
}

/* 2: Whole-strip hue spectrum. */
static void zmk_rgb_underglow_effect_spectrum(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        struct zmk_led_hsb hsb = state.color;
        hsb.h = state.animation_step;
        pixels[i] = hsb_to_rgb(hsb_scale_min_max(hsb));
    }

    state.animation_step = (state.animation_step + state.animation_speed) % HUE_MAX;
}

/* 3: Per-pixel rainbow swirl along the physical LED chain. */
static void zmk_rgb_underglow_effect_swirl(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        struct zmk_led_hsb hsb = state.color;
        hsb.h = (HUE_MAX / STRIP_NUM_PIXELS * i + state.animation_step) % HUE_MAX;
        pixels[i] = hsb_to_rgb(hsb_scale_min_max(hsb));
    }

    state.animation_step = (state.animation_step + state.animation_speed * 2U) % HUE_MAX;
}

/* 4: Static seven-column rainbow. HUI/HUD rotates the palette. */
static void zmk_rgb_underglow_effect_column_rainbow(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const int hue = state.color.h + (HUE_MAX * col) / EYELASH_LOGICAL_COLUMNS;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }
}

/* 5: Moving rainbow wave across seven logical columns. */
static void zmk_rgb_underglow_effect_rainbow_wave(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const int hue = state.color.h + (HUE_MAX * col) / EYELASH_LOGICAL_COLUMNS +
                        state.animation_step;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % HUE_MAX;
}

/* 6: Smooth Scanner / Cylon with a continuous sub-column position. */
static void zmk_rgb_underglow_effect_scanner(void) {
    const uint16_t sub = 32U;
    const uint16_t edge = (EYELASH_LOGICAL_COLUMNS - 1U) * sub;
    const uint16_t path = edge * 2U;
    uint16_t phase = state.animation_step % path;
    uint16_t head = (phase <= edge) ? phase : (path - phase);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t col_pos = eyelash_logical_column(i) * sub;
        const uint16_t distance = (head > col_pos) ? (head - col_pos) : (col_pos - head);
        const uint8_t pct = eyelash_peak_percent(distance, sub * 3U, 3U);
        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % path;
}

/* 7: Soft ring travels from the center toward both edges. */
static void zmk_rgb_underglow_effect_center_out(void) {
    const uint16_t sub = 32U;
    const uint8_t center = EYELASH_LOGICAL_COLUMNS / 2U;
    const uint16_t max_radius = center * sub;
    const uint16_t cycle = max_radius + sub * 2U;
    const uint16_t ring = state.animation_step % cycle;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t distance =
            (uint16_t)abs((int)eyelash_logical_column(i) - (int)center) * sub;
        const uint16_t delta = (distance > ring) ? (distance - ring) : (ring - distance);
        const uint8_t pct = eyelash_peak_percent(delta, sub + sub / 2U, 2U);
        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % cycle;
}

/* 8: Continuous meteor with a soft directed tail. */
static void zmk_rgb_underglow_effect_meteor(void) {
    const uint16_t sub = 32U;
    const uint16_t period = EYELASH_LOGICAL_COLUMNS * sub;
    const uint16_t head = state.animation_step % period;
    const uint16_t tail = sub * 4U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t behind = (head + period - pos) % period;
        uint8_t pct = 2U;

        if (behind <= tail) {
            pct = 8U + (uint8_t)(((uint32_t)92U * (tail - behind)) / tail);
        }

        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % period;
}

/* 9: Soft user-color brightness wave across columns. */
static void zmk_rgb_underglow_effect_pulse_wave(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint16_t phase =
            (state.animation_step + (200U * col) / EYELASH_LOGICAL_COLUMNS) % 200U;
        const uint8_t wave = eyelash_triangle_percent(phase, 200U);
        const uint8_t pct = 12U + (uint8_t)((wave * 88U) / 100U);
        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed * 2U) % 200U;
}

/* 10: Two alternating hue families smoothly trade places. */
static void zmk_rgb_underglow_effect_alternating(void) {
    const uint8_t mix = eyelash_triangle_percent(state.animation_step, 200U);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const bool odd = (col & 1U) != 0;
        const int offset = odd ? (180 - (180 * mix) / 100) : ((180 * mix) / 100);
        pixels[i] = eyelash_effect_rgb(state.color.h + offset, state.color.s, 100U);
    }

    state.animation_step = (state.animation_step + state.animation_speed * 2U) % 200U;
}

/* 11: Compact moving hue gradient. */
static void zmk_rgb_underglow_effect_moving_gradient(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const int hue = state.color.h + col * 28 + state.animation_step;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % HUE_MAX;
}

/* 12: Slow neighboring hue/brightness curtains. */
static void zmk_rgb_underglow_effect_aurora(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint16_t phase = (state.animation_step + col * 23U + row * 11U) % 180U;
        const uint8_t wave = eyelash_triangle_percent(phase, 180U);
        const int hue_offset = ((int)wave - 50) / 2;
        const int hue = state.color.h + col * 8 + hue_offset;
        const uint8_t pct = 38U + (uint8_t)((wave * 62U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 180U;
}

/* 13: Column-synchronized sparkle, now interpolated between random keyframes. */
static void zmk_rgb_underglow_effect_sparkle(void) {
    const uint16_t key_len = 18U;
    const uint32_t frame = state.animation_step / key_len;
    const uint8_t frac =
        (uint8_t)(((state.animation_step % key_len) * 100U) / key_len);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint32_t seed = col * 0x85ebca6bU;
        const uint32_t r0 = eyelash_hash32(seed + frame * 0x9e3779b9U);
        const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x9e3779b9U);

        const uint8_t a = ((r0 & 0x7U) < 2U) ? 100U : (uint8_t)(12U + ((r0 >> 8) % 18U));
        const uint8_t b = ((r1 & 0x7U) < 2U) ? 100U : (uint8_t)(12U + ((r1 >> 8) % 18U));
        const uint8_t pct = eyelash_lerp_u8(a, b, frac);
        const int hue = state.color.h + (int)((r0 >> 16) % 25U) - 12;

        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 14: Warm flame with slow interpolated heat variation. */
static void zmk_rgb_underglow_effect_fire(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint32_t seed = col * 0x165667b1U + row * 0x27d4eb2dU;
        const uint8_t pct =
            eyelash_interpolated_random_level(seed, state.animation_step, 14U, 42U, 100U);
        const uint32_t hue_rnd =
            eyelash_hash32(seed + (state.animation_step / 14U) * 0x9e3779b9U);
        const int hue = 4 + (int)(hue_rnd % 40U);

        pixels[i] = eyelash_effect_rgb(hue, 100U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 15: Smooth green Matrix head with a fading directed tail. */
static void zmk_rgb_underglow_effect_matrix(void) {
    const uint16_t sub = 32U;
    const uint16_t period = EYELASH_LOGICAL_COLUMNS * sub;
    const uint16_t head = state.animation_step % period;
    const uint16_t tail = sub * 4U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t behind = (head + period - pos) % period;
        uint8_t pct = 2U;

        if (behind <= tail) {
            pct = 6U + (uint8_t)(((uint32_t)94U * (tail - behind)) / tail);
        }

        pixels[i] = eyelash_effect_rgb(120, 100, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % period;
}

/* --------------------------------------------------------------------------
 * New smooth effects 16..35.
 * -------------------------------------------------------------------------- */

/* 16: Low-saturation pastel color flow. */
static void zmk_rgb_underglow_effect_pastel_flow(void) {
    const uint8_t sat = MIN(state.color.s, 48U);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const int hue = state.color.h + col * 24 + row * 12 + state.animation_step;
        const uint8_t wave =
            eyelash_triangle_percent(state.animation_step + col * 17U + row * 9U, 220U);
        const uint8_t pct = 72U + (uint8_t)((wave * 28U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, sat, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % HUE_MAX;
}

/* 17: Blue/teal ocean swell. */
static void zmk_rgb_underglow_effect_ocean(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t wave =
            eyelash_triangle_percent(state.animation_step + col * 22U + row * 37U, 240U);
        const int hue = 175 + (int)((wave * 55U) / 100U);
        const uint8_t pct = 28U + (uint8_t)((wave * 72U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, 88U, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 240U;
}

/* 18: Warm sunset gradient, slowly breathing. */
static void zmk_rgb_underglow_effect_sunset(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const int hue = 330 + (55 * col) / (EYELASH_LOGICAL_COLUMNS - 1U) + row * 3;
        const uint8_t wave =
            eyelash_triangle_percent(state.animation_step + col * 8U, 260U);
        const uint8_t pct = 62U + (uint8_t)((wave * 38U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, 92U, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 260U;
}

/* 19: Red/orange lava currents. */
static void zmk_rgb_underglow_effect_lava(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t wave1 =
            eyelash_triangle_percent(state.animation_step + col * 29U + row * 41U, 220U);
        const uint8_t wave2 =
            eyelash_triangle_percent(state.animation_step * 2U + col * 13U, 170U);
        const uint8_t mix = (uint8_t)(((uint16_t)wave1 + wave2) / 2U);
        const int hue = 2 + (int)((mix * 42U) / 100U);
        const uint8_t pct = 30U + (uint8_t)((mix * 70U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, 100U, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 600U;
}

/* 20: Deep green forest canopy movement. */
static void zmk_rgb_underglow_effect_forest(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t wave =
            eyelash_triangle_percent(state.animation_step + col * 31U + row * 47U, 300U);
        const int hue = 92 + (int)((wave * 48U) / 100U);
        const uint8_t pct = 22U + (uint8_t)((wave * 68U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, 90U, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 300U;
}

/* 21: Multi-axis plasma using two smooth triangle fields. */
static void zmk_rgb_underglow_effect_plasma(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t a =
            eyelash_triangle_percent(state.animation_step + col * 35U + row * 21U, 240U);
        const uint8_t b =
            eyelash_triangle_percent(state.animation_step * 2U + col * 17U + (3U - row) * 29U, 190U);
        const uint8_t mix = (uint8_t)(((uint16_t)a + b) / 2U);
        const int hue = state.color.h + (int)((mix * 240U) / 100U);
        const uint8_t pct = 45U + (uint8_t)((mix * 55U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, MAX(state.color.s, 70U), pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 1000U;
}

/* 22: Long single comet, softer and longer than Meteor. */
static void zmk_rgb_underglow_effect_comet(void) {
    const uint16_t sub = 32U;
    const uint16_t period = EYELASH_LOGICAL_COLUMNS * sub;
    const uint16_t head = state.animation_step % period;
    const uint16_t tail = sub * 6U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t behind = (head + period - pos) % period;
        const uint8_t pct =
            (behind <= tail) ? (8U + (uint8_t)(((uint32_t)92U * (tail - behind)) / tail)) : 4U;
        const int hue = state.color.h + (int)(eyelash_logical_row(i) * 5U);
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % period;
}

/* 23: Two opposite comets orbiting the strip. */
static void zmk_rgb_underglow_effect_dual_comet(void) {
    const uint16_t sub = 32U;
    const uint16_t period = EYELASH_LOGICAL_COLUMNS * sub;
    const uint16_t head1 = state.animation_step % period;
    const uint16_t head2 = (head1 + period / 2U) % period;
    const uint16_t tail = sub * 3U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t d1 = (head1 + period - pos) % period;
        const uint16_t d2 = (head2 + period - pos) % period;
        uint8_t p1 = (d1 <= tail) ? (uint8_t)(10U + (90U * (tail - d1)) / tail) : 3U;
        uint8_t p2 = (d2 <= tail) ? (uint8_t)(10U + (90U * (tail - d2)) / tail) : 3U;
        const uint8_t pct = MAX(p1, p2);
        const int hue = state.color.h + ((p2 > p1) ? 80 : 0);
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % period;
}

/* 24: Two mirrored scanner heads. */
static void zmk_rgb_underglow_effect_dual_scanner(void) {
    const uint16_t sub = 32U;
    const uint16_t edge = (EYELASH_LOGICAL_COLUMNS - 1U) * sub;
    const uint16_t path = edge * 2U;
    uint16_t phase = state.animation_step % path;
    uint16_t head1 = (phase <= edge) ? phase : (path - phase);
    uint16_t head2 = edge - head1;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t d1 = (head1 > pos) ? (head1 - pos) : (pos - head1);
        const uint16_t d2 = (head2 > pos) ? (head2 - pos) : (pos - head2);
        const uint8_t p1 = eyelash_peak_percent(d1, sub * 2U, 2U);
        const uint8_t p2 = eyelash_peak_percent(d2, sub * 2U, 2U);
        const uint8_t pct = MAX(p1, p2);
        pixels[i] = eyelash_effect_rgb(state.color.h + ((p2 > p1) ? 55 : 0),
                                       state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % path;
}

/* 25: Two bright fronts move from outer edges to the center and back. */
static void zmk_rgb_underglow_effect_edge_in(void) {
    const uint16_t sub = 32U;
    const uint16_t edge = (EYELASH_LOGICAL_COLUMNS - 1U) * sub;
    const uint16_t half_edge = edge / 2U;
    const uint16_t cycle = half_edge * 2U;
    uint16_t phase = state.animation_step % cycle;
    uint16_t inset = (phase <= half_edge) ? phase : (cycle - phase);
    uint16_t left = inset;
    uint16_t right = edge - inset;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t pos = eyelash_logical_column(i) * sub;
        const uint16_t dl = (left > pos) ? (left - pos) : (pos - left);
        const uint16_t dr = (right > pos) ? (right - pos) : (pos - right);
        const uint8_t pct = MAX(eyelash_peak_percent(dl, sub * 2U, 4U),
                                eyelash_peak_percent(dr, sub * 2U, 4U));
        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % cycle;
}

/* 26: Center glow smoothly expands and contracts. */
static void zmk_rgb_underglow_effect_center_glow(void) {
    const uint8_t center = EYELASH_LOGICAL_COLUMNS / 2U;
    const uint8_t wave = eyelash_triangle_percent(state.animation_step, 240U);
    const uint16_t radius = 24U + (uint16_t)((wave * 120U) / 100U);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint16_t distance =
            (uint16_t)abs((int)eyelash_logical_column(i) - (int)center) * 32U;
        const uint8_t pct = eyelash_peak_percent(distance, radius, 8U);
        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed * 2U) % 240U;
}

/* 27: Repeating soft ripple bands centered on the middle column. */
static void zmk_rgb_underglow_effect_ripple(void) {
    const uint8_t center = EYELASH_LOGICAL_COLUMNS / 2U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t dist =
            (uint8_t)abs((int)eyelash_logical_column(i) - (int)center);
        const uint16_t phase = (state.animation_step + dist * 46U) % 220U;
        const uint8_t wave = eyelash_triangle_percent(phase, 220U);
        const uint8_t pct = 8U + (uint8_t)((wave * 92U) / 100U);
        const int hue = state.color.h + dist * 12;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed * 2U) % 220U;
}

/* 28: Independent per-LED smooth twinkles. */
static void zmk_rgb_underglow_effect_twinkle(void) {
    const uint16_t key_len = 28U;
    const uint32_t frame = state.animation_step / key_len;
    const uint8_t frac =
        (uint8_t)(((state.animation_step % key_len) * 100U) / key_len);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint32_t seed = (uint32_t)i * 0x27d4eb2dU;
        const uint32_t r0 = eyelash_hash32(seed + frame * 0x9e3779b9U);
        const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x9e3779b9U);
        const uint8_t a = ((r0 & 0x0FU) < 3U) ? (uint8_t)(65U + ((r0 >> 8) % 36U))
                                             : (uint8_t)(6U + ((r0 >> 8) % 12U));
        const uint8_t b = ((r1 & 0x0FU) < 3U) ? (uint8_t)(65U + ((r1 >> 8) % 36U))
                                             : (uint8_t)(6U + ((r1 >> 8) % 12U));
        const uint8_t pct = eyelash_lerp_u8(a, b, frac);
        const int hue = state.color.h + (int)((r0 >> 16) % 41U) - 20;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 29: Sparse slow green/gold fireflies on a dark background. */
static void zmk_rgb_underglow_effect_fireflies(void) {
    const uint16_t key_len = 36U;
    const uint32_t frame = state.animation_step / key_len;
    const uint8_t frac =
        (uint8_t)(((state.animation_step % key_len) * 100U) / key_len);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint32_t seed = (uint32_t)i * 0x165667b1U + 0x1234U;
        const uint32_t r0 = eyelash_hash32(seed + frame * 0x85ebca6bU);
        const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x85ebca6bU);
        const uint8_t a = ((r0 & 0x1FU) < 5U) ? (uint8_t)(55U + ((r0 >> 8) % 46U)) : 3U;
        const uint8_t b = ((r1 & 0x1FU) < 5U) ? (uint8_t)(55U + ((r1 >> 8) % 46U)) : 3U;
        const uint8_t pct = eyelash_lerp_u8(a, b, frac);
        const int hue = 70 + (int)((r0 >> 16) % 55U);
        pixels[i] = eyelash_effect_rgb(hue, 92U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 30: Gentle warm candlelight, no hard flicker. */
static void zmk_rgb_underglow_effect_candle(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint32_t seed = (uint32_t)i * 0x85ebca6bU;
        const uint8_t pct =
            eyelash_interpolated_random_level(seed, state.animation_step, 24U, 52U, 88U);
        const uint32_t r =
            eyelash_hash32(seed + (state.animation_step / 24U) * 0x27d4eb2dU);
        const int hue = 24 + (int)(r % 14U);
        pixels[i] = eyelash_effect_rgb(hue, 88U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 31: Sparse red/orange embers over a very dim warm bed. */
static void zmk_rgb_underglow_effect_embers(void) {
    const uint16_t key_len = 32U;
    const uint32_t frame = state.animation_step / key_len;
    const uint8_t frac =
        (uint8_t)(((state.animation_step % key_len) * 100U) / key_len);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint32_t seed = (uint32_t)i * 0x9e3779b9U;
        const uint32_t r0 = eyelash_hash32(seed + frame * 0x165667b1U);
        const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x165667b1U);
        const uint8_t a = ((r0 & 0x0FU) < 4U) ? (uint8_t)(50U + ((r0 >> 8) % 51U)) : 8U;
        const uint8_t b = ((r1 & 0x0FU) < 4U) ? (uint8_t)(50U + ((r1 >> 8) % 51U)) : 8U;
        const uint8_t pct = eyelash_lerp_u8(a, b, frac);
        const int hue = 2 + (int)((r0 >> 16) % 32U);
        pixels[i] = eyelash_effect_rgb(hue, 100U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 32: Blue/cyan drops move smoothly through the three logical rows. */
static void zmk_rgb_underglow_effect_soft_rain(void) {
    const uint16_t sub = 40U;
    const uint16_t rows = STRIP_NUM_PIXELS / EYELASH_LOGICAL_COLUMNS;
    const uint16_t period = rows * sub;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint16_t head =
            (state.animation_step + col * 23U) % period;
        const uint16_t pos = row * sub;
        const uint16_t behind = (head + period - pos) % period;
        uint8_t pct = 5U;

        if (behind <= sub * 2U) {
            pct = 10U + (uint8_t)((90U * (sub * 2U - behind)) / (sub * 2U));
        }

        const int hue = 185 + col * 4;
        pixels[i] = eyelash_effect_rgb(hue, 82U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % period;
}

/* 33: Purple/blue galaxy with slow independent stars. */
static void zmk_rgb_underglow_effect_galaxy(void) {
    const uint16_t key_len = 40U;
    const uint32_t frame = state.animation_step / key_len;
    const uint8_t frac =
        (uint8_t)(((state.animation_step % key_len) * 100U) / key_len);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint32_t seed = (uint32_t)i * 0x85ebca6bU + 0x777U;
        const uint32_t r0 = eyelash_hash32(seed + frame * 0x27d4eb2dU);
        const uint32_t r1 = eyelash_hash32(seed + (frame + 1U) * 0x27d4eb2dU);
        const uint8_t a = ((r0 & 0x1FU) < 4U) ? (uint8_t)(60U + ((r0 >> 8) % 41U))
                                             : (uint8_t)(15U + ((col + row) % 12U));
        const uint8_t b = ((r1 & 0x1FU) < 4U) ? (uint8_t)(60U + ((r1 >> 8) % 41U))
                                             : (uint8_t)(15U + ((col + row) % 12U));
        const uint8_t pct = eyelash_lerp_u8(a, b, frac);
        const int hue = 225 + (int)((r0 >> 16) % 70U);
        pixels[i] = eyelash_effect_rgb(hue, 82U, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % 60000U;
}

/* 34: Near-solid user color with a subtle traveling shimmer. */
static void zmk_rgb_underglow_effect_shimmer(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t wave =
            eyelash_triangle_percent(state.animation_step + col * 21U + row * 17U, 180U);
        const uint8_t pct = 68U + (uint8_t)((wave * 32U) / 100U);
        const int hue = state.color.h + ((int)wave - 50) / 10;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 180U;
}

/* 35: Fixed green/cyan/blue northern-light curtains. */
static void zmk_rgb_underglow_effect_northern_lights(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t row = eyelash_logical_row(i);
        const uint8_t wave1 =
            eyelash_triangle_percent(state.animation_step + col * 27U + row * 19U, 260U);
        const uint8_t wave2 =
            eyelash_triangle_percent(state.animation_step * 2U + col * 11U, 210U);
        const uint8_t mix = (uint8_t)(((uint16_t)wave1 + wave2) / 2U);
        const int hue = 115 + (int)((mix * 95U) / 100U);
        const uint8_t pct = 24U + (uint8_t)((mix * 76U) / 100U);
        pixels[i] = eyelash_effect_rgb(hue, 90U, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 1000U;
}

static void zmk_rgb_underglow_tick(struct k_work *work) {
    switch (state.current_effect) {
    case UNDERGLOW_EFFECT_SOLID:
        zmk_rgb_underglow_effect_solid();
        break;
    case UNDERGLOW_EFFECT_BREATHE:
        zmk_rgb_underglow_effect_breathe();
        break;
    case UNDERGLOW_EFFECT_SPECTRUM:
        zmk_rgb_underglow_effect_spectrum();
        break;
    case UNDERGLOW_EFFECT_SWIRL:
        zmk_rgb_underglow_effect_swirl();
        break;
    case UNDERGLOW_EFFECT_COLUMN_RAINBOW:
        zmk_rgb_underglow_effect_column_rainbow();
        break;
    case UNDERGLOW_EFFECT_RAINBOW_WAVE:
        zmk_rgb_underglow_effect_rainbow_wave();
        break;
    case UNDERGLOW_EFFECT_SCANNER:
        zmk_rgb_underglow_effect_scanner();
        break;
    case UNDERGLOW_EFFECT_CENTER_OUT:
        zmk_rgb_underglow_effect_center_out();
        break;
    case UNDERGLOW_EFFECT_METEOR:
        zmk_rgb_underglow_effect_meteor();
        break;
    case UNDERGLOW_EFFECT_PULSE_WAVE:
        zmk_rgb_underglow_effect_pulse_wave();
        break;
    case UNDERGLOW_EFFECT_ALTERNATING:
        zmk_rgb_underglow_effect_alternating();
        break;
    case UNDERGLOW_EFFECT_MOVING_GRADIENT:
        zmk_rgb_underglow_effect_moving_gradient();
        break;
    case UNDERGLOW_EFFECT_AURORA:
        zmk_rgb_underglow_effect_aurora();
        break;
    case UNDERGLOW_EFFECT_SPARKLE:
        zmk_rgb_underglow_effect_sparkle();
        break;
    case UNDERGLOW_EFFECT_FIRE:
        zmk_rgb_underglow_effect_fire();
        break;
    case UNDERGLOW_EFFECT_MATRIX:
        zmk_rgb_underglow_effect_matrix();
        break;
    case UNDERGLOW_EFFECT_PASTEL_FLOW:
        zmk_rgb_underglow_effect_pastel_flow();
        break;
    case UNDERGLOW_EFFECT_OCEAN:
        zmk_rgb_underglow_effect_ocean();
        break;
    case UNDERGLOW_EFFECT_SUNSET:
        zmk_rgb_underglow_effect_sunset();
        break;
    case UNDERGLOW_EFFECT_LAVA:
        zmk_rgb_underglow_effect_lava();
        break;
    case UNDERGLOW_EFFECT_FOREST:
        zmk_rgb_underglow_effect_forest();
        break;
    case UNDERGLOW_EFFECT_PLASMA:
        zmk_rgb_underglow_effect_plasma();
        break;
    case UNDERGLOW_EFFECT_COMET:
        zmk_rgb_underglow_effect_comet();
        break;
    case UNDERGLOW_EFFECT_DUAL_COMET:
        zmk_rgb_underglow_effect_dual_comet();
        break;
    case UNDERGLOW_EFFECT_DUAL_SCANNER:
        zmk_rgb_underglow_effect_dual_scanner();
        break;
    case UNDERGLOW_EFFECT_EDGE_IN:
        zmk_rgb_underglow_effect_edge_in();
        break;
    case UNDERGLOW_EFFECT_CENTER_GLOW:
        zmk_rgb_underglow_effect_center_glow();
        break;
    case UNDERGLOW_EFFECT_RIPPLE:
        zmk_rgb_underglow_effect_ripple();
        break;
    case UNDERGLOW_EFFECT_TWINKLE:
        zmk_rgb_underglow_effect_twinkle();
        break;
    case UNDERGLOW_EFFECT_FIREFLIES:
        zmk_rgb_underglow_effect_fireflies();
        break;
    case UNDERGLOW_EFFECT_CANDLE:
        zmk_rgb_underglow_effect_candle();
        break;
    case UNDERGLOW_EFFECT_EMBERS:
        zmk_rgb_underglow_effect_embers();
        break;
    case UNDERGLOW_EFFECT_SOFT_RAIN:
        zmk_rgb_underglow_effect_soft_rain();
        break;
    case UNDERGLOW_EFFECT_GALAXY:
        zmk_rgb_underglow_effect_galaxy();
        break;
    case UNDERGLOW_EFFECT_SHIMMER:
        zmk_rgb_underglow_effect_shimmer();
        break;
    case UNDERGLOW_EFFECT_NORTHERN_LIGHTS:
        zmk_rgb_underglow_effect_northern_lights();
        break;
    default:
        zmk_rgb_underglow_effect_solid();
        break;
    }

    int err = eyelash_flush_smoothed_frame();
    if (err < 0) {
        LOG_ERR("Failed to update the RGB strip (%d)", err);
    }
}

K_WORK_DEFINE(underglow_tick_work, zmk_rgb_underglow_tick);

static void zmk_rgb_underglow_tick_handler(struct k_timer *timer) {
    if (!state.on) {
        return;
    }

    k_work_submit_to_queue(zmk_workqueue_lowprio_work_q(), &underglow_tick_work);
}

K_TIMER_DEFINE(underglow_tick, zmk_rgb_underglow_tick_handler, NULL);
#if IS_ENABLED(CONFIG_SETTINGS)
static int rgb_settings_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg) {
    const char *next;
    int rc;

    if (settings_name_steq(name, "state", &next) && !next) {
        if (len != sizeof(state)) {
            return -EINVAL;
        }

        rc = read_cb(cb_arg, &state, sizeof(state));
        if (rc >= 0) {
            /* Keep settings robust across experiments/downgrades/corruption. */
            if (state.current_effect >= UNDERGLOW_EFFECT_NUMBER) {
                state.current_effect = UNDERGLOW_EFFECT_SOLID;
                state.animation_step = 0;
            }
            if (state.on) {
                k_timer_start(&underglow_tick, K_NO_WAIT, K_MSEC(50));
            }
            return 0;
        }

        return rc;
    }

    return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(rgb_underglow, "rgb/underglow", NULL, rgb_settings_set, NULL, NULL);

static void zmk_rgb_underglow_save_state_work(struct k_work *_work) {
    settings_save_one("rgb/underglow/state", &state, sizeof(state));
}

static struct k_work_delayable underglow_save_work;
#endif

static int zmk_rgb_underglow_init(void) {
    led_strip = DEVICE_DT_GET(STRIP_CHOSEN);
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_EXT_POWER)
    if (!device_is_ready(ext_power)) {
        LOG_ERR("External power device \"%s\" is not ready", ext_power->name);
        return -ENODEV;
    }
#endif
    state = (struct rgb_underglow_state){
        color : {
            h : CONFIG_ZMK_RGB_UNDERGLOW_HUE_START,
            s : CONFIG_ZMK_RGB_UNDERGLOW_SAT_START,
            b : CONFIG_ZMK_RGB_UNDERGLOW_BRT_START,
        },
        animation_speed : CONFIG_ZMK_RGB_UNDERGLOW_SPD_START,
        current_effect : CONFIG_ZMK_RGB_UNDERGLOW_EFF_START,
        animation_step : 0,
        on : IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_ON_START)
    };
#if IS_ENABLED(CONFIG_SETTINGS)
    k_work_init_delayable(&underglow_save_work, zmk_rgb_underglow_save_state_work);
#endif
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_USB)
    state.on = zmk_usb_is_powered();
#endif

    if (state.on) {
        k_timer_start(&underglow_tick, K_NO_WAIT, K_MSEC(50));
    }

    return 0;
}

int zmk_rgb_underglow_save_state(void) {
#if IS_ENABLED(CONFIG_SETTINGS)
    int ret = k_work_reschedule(&underglow_save_work, K_MSEC(CONFIG_ZMK_SETTINGS_SAVE_DEBOUNCE));
    return MIN(ret, 0);
#else
    return 0;
#endif
}
int zmk_rgb_underglow_get_state(bool *on_off) {
    if (!led_strip)
        return -ENODEV;

    *on_off = state.on;
    return 0;
}

int zmk_rgb_underglow_on(void) {
    if (!led_strip)
        return -ENODEV;
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_EXT_POWER)
    if (ext_power != NULL) {
        int rc = ext_power_enable(ext_power);
        if (rc != 0) {
            LOG_ERR("Unable to enable EXT_POWER: %d", rc);
        }
    }
#endif

    state.on = true;
    state.animation_step = 0;
    k_timer_start(&underglow_tick, K_NO_WAIT, K_MSEC(50));

    return zmk_rgb_underglow_save_state();
}
static void zmk_rgb_underglow_off_handler(struct k_work *work) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        pixels[i] = (struct led_rgb){r : 0, g : 0, b : 0};
        smoothed_pixels[i] = (struct led_rgb){r : 0, g : 0, b : 0};
    }

    smoothed_pixels_initialized = true;
    led_strip_update_rgb(led_strip, smoothed_pixels, STRIP_NUM_PIXELS);
}

K_WORK_DEFINE(underglow_off_work, zmk_rgb_underglow_off_handler);

int zmk_rgb_underglow_off(void) {
    if (!led_strip)
        return -ENODEV;
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_EXT_POWER)
    if (ext_power != NULL) {
        int rc = ext_power_disable(ext_power);
        if (rc != 0) {
            LOG_ERR("Unable to disable EXT_POWER: %d", rc);
        }
    }
#endif

    k_work_submit_to_queue(zmk_workqueue_lowprio_work_q(), &underglow_off_work);

    k_timer_stop(&underglow_tick);
    state.on = false;

    return zmk_rgb_underglow_save_state();
}
int zmk_rgb_underglow_calc_effect(int direction) {
    const int count = sizeof(eyelash_effect_cycle_order) / sizeof(eyelash_effect_cycle_order[0]);
    int current_index = 0;

    for (int i = 0; i < count; i++) {
        if (eyelash_effect_cycle_order[i] == state.current_effect) {
            current_index = i;
            break;
        }
    }

    int next = current_index + direction;
    while (next < 0) {
        next += count;
    }
    next %= count;

    return eyelash_effect_cycle_order[next];
}

int zmk_rgb_underglow_select_effect(int effect) {
    if (!led_strip)
        return -ENODEV;

    if (effect < 0 || effect >= UNDERGLOW_EFFECT_NUMBER) {
        return -EINVAL;
    }

    state.current_effect = effect;
    state.animation_step = 0;

    return zmk_rgb_underglow_save_state();
}
int zmk_rgb_underglow_cycle_effect(int direction) {
    return zmk_rgb_underglow_select_effect(zmk_rgb_underglow_calc_effect(direction));
}

int zmk_rgb_underglow_toggle(void) {
    return state.on ? zmk_rgb_underglow_off() : zmk_rgb_underglow_on();
}

int zmk_rgb_underglow_set_hsb(struct zmk_led_hsb color) {
    if (color.h > HUE_MAX || color.s > SAT_MAX || color.b > BRT_MAX) {
        return -ENOTSUP;
    }

    state.color = color;

    return 0;
}
struct zmk_led_hsb zmk_rgb_underglow_calc_hue(int direction) {
    struct zmk_led_hsb color = state.color;

    color.h += HUE_MAX + (direction * CONFIG_ZMK_RGB_UNDERGLOW_HUE_STEP);
    color.h %= HUE_MAX;

    return color;
}

struct zmk_led_hsb zmk_rgb_underglow_calc_sat(int direction) {
    struct zmk_led_hsb color = state.color;
    int s = color.s + (direction * CONFIG_ZMK_RGB_UNDERGLOW_SAT_STEP);
    if (s < 0) {
        s = 0;
    } else if (s > SAT_MAX) {
        s = SAT_MAX;
    }
    color.s = s;

    return color;
}

struct zmk_led_hsb zmk_rgb_underglow_calc_brt(int direction) {
    struct zmk_led_hsb color = state.color;

    int b = color.b + (direction * CONFIG_ZMK_RGB_UNDERGLOW_BRT_STEP);
    color.b = CLAMP(b, 0, BRT_MAX);

    return color;
}
int zmk_rgb_underglow_change_hue(int direction) {
    if (!led_strip)
        return -ENODEV;

    state.color = zmk_rgb_underglow_calc_hue(direction);

    return zmk_rgb_underglow_save_state();
}

int zmk_rgb_underglow_change_sat(int direction) {
    if (!led_strip)
        return -ENODEV;

    state.color = zmk_rgb_underglow_calc_sat(direction);

    return zmk_rgb_underglow_save_state();
}

int zmk_rgb_underglow_change_brt(int direction) {
    if (!led_strip)
        return -ENODEV;
    state.color = zmk_rgb_underglow_calc_brt(direction);

    return zmk_rgb_underglow_save_state();
}

int zmk_rgb_underglow_change_spd(int direction) {
    if (!led_strip)
        return -ENODEV;

    if (state.animation_speed == 1 && direction < 0) {
        return 0;
    }

    state.animation_speed += direction;

    if (state.animation_speed > 5) {
        state.animation_speed = 5;
    }

    return zmk_rgb_underglow_save_state();
}
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_IDLE) ||                                          \
    IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_USB)
struct rgb_underglow_sleep_state {
    bool is_awake;
    bool rgb_state_before_sleeping;
};

static int rgb_underglow_auto_state(bool target_wake_state) {
    static struct rgb_underglow_sleep_state sleep_state = {
        is_awake : true,
        rgb_state_before_sleeping : false
    };
    // wake up event while awake, or sleep event while sleeping -> no-op
    if (target_wake_state == sleep_state.is_awake) {
        return 0;
    }
    sleep_state.is_awake = target_wake_state;
    if (sleep_state.is_awake) {
        if (sleep_state.rgb_state_before_sleeping) {
            return zmk_rgb_underglow_on();
        } else {
            return zmk_rgb_underglow_off();
        }
    } else {
        sleep_state.rgb_state_before_sleeping = state.on;
        return zmk_rgb_underglow_off();
    }
}

static int rgb_underglow_event_listener(const zmk_event_t *eh) {
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_IDLE)
    if (as_zmk_activity_state_changed(eh)) {
        return rgb_underglow_auto_state(zmk_activity_get_state() == ZMK_ACTIVITY_ACTIVE);
    }
#endif
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_USB)
    if (as_zmk_usb_conn_state_changed(eh)) {
        return rgb_underglow_auto_state(zmk_usb_is_powered());
    }
#endif

    return -ENOTSUP;
}

ZMK_LISTENER(rgb_underglow, rgb_underglow_event_listener);
#endif // IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_IDLE) ||
       // IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_USB)

#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_IDLE)
ZMK_SUBSCRIPTION(rgb_underglow, zmk_activity_state_changed);
#endif
#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW_AUTO_OFF_USB)
ZMK_SUBSCRIPTION(rgb_underglow, zmk_usb_conn_state_changed);
#endif

SYS_INIT(zmk_rgb_underglow_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);