/*
 * Copyright (c) 2020 The ZMK Contributors
 *
 * SPDX-License-Identifier: MIT
 *
 * Eyelash Corne RGB16 extension:
 * - effects 0..3 are the original cormoran/zmk v0.3-branch+dya effects
 * - effects 4..15 are additional 7-logical-column effects for the 21 LED wiring
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
    /* Keep these four IDs unchanged. */
    UNDERGLOW_EFFECT_SOLID = 0,
    UNDERGLOW_EFFECT_BREATHE,
    UNDERGLOW_EFFECT_SPECTRUM,
    UNDERGLOW_EFFECT_SWIRL,

    /* Eyelash RGB16 extension. */
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

    UNDERGLOW_EFFECT_NUMBER // Used to track number of underglow effects
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
 * Original ZMK/cormoran effects 0..3. Keep their behavior unchanged.
 * -------------------------------------------------------------------------- */

static void zmk_rgb_underglow_effect_solid(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        pixels[i] = hsb_to_rgb(hsb_scale_min_max(state.color));
    }
}

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

static void zmk_rgb_underglow_effect_spectrum(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        struct zmk_led_hsb hsb = state.color;
        hsb.h = state.animation_step;

        pixels[i] = hsb_to_rgb(hsb_scale_min_max(hsb));
    }
    state.animation_step += state.animation_speed;
    state.animation_step = state.animation_step % HUE_MAX;
}

static void zmk_rgb_underglow_effect_swirl(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        struct zmk_led_hsb hsb = state.color;
        hsb.h = (HUE_MAX / STRIP_NUM_PIXELS * i + state.animation_step) % HUE_MAX;

        pixels[i] = hsb_to_rgb(hsb_scale_min_max(hsb));
    }
    state.animation_step += state.animation_speed * 2;
    state.animation_step = state.animation_step % HUE_MAX;
}

/* --------------------------------------------------------------------------
 * Eyelash-specific helpers for effects 4..15.
 * -------------------------------------------------------------------------- */

static inline uint8_t eyelash_logical_column(int pixel_index) {
    return (uint8_t)(pixel_index % EYELASH_LOGICAL_COLUMNS);
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
    const uint16_t half = period / 2;

    if (phase <= half) {
        return (uint8_t)((phase * 100U) / half);
    }

    return (uint8_t)(((period - phase) * 100U) / (period - half));
}

/* Small deterministic hash. It lets Sparkle/Fire look random without keeping
 * extra state, so the persisted rgb_underglow_state layout remains unchanged.
 */
static uint32_t eyelash_hash32(uint32_t x) {
    x ^= x >> 16;
    x *= 0x7feb352dU;
    x ^= x >> 15;
    x *= 0x846ca68bU;
    x ^= x >> 16;
    return x;
}

/* brightness_percent is relative to the user's current RGB brightness. */
static struct led_rgb eyelash_effect_rgb(int hue, uint8_t saturation, uint8_t brightness_percent) {
    struct zmk_led_hsb hsb = state.color;

    if (saturation > SAT_MAX) {
        saturation = SAT_MAX;
    }
    if (brightness_percent > 100) {
        brightness_percent = 100;
    }

    hsb.h = eyelash_wrap_hue(hue);
    hsb.s = saturation;
    hsb.b = (uint8_t)(((uint16_t)state.color.b * brightness_percent) / 100U);

    return hsb_to_rgb(hsb_scale_zero_max(hsb));
}

/* 4: Static seven-column rainbow. HUI/HUD rotates the whole palette. */
static void zmk_rgb_underglow_effect_column_rainbow(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const int hue = state.color.h + (HUE_MAX * col) / EYELASH_LOGICAL_COLUMNS;
        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }
}

/* 5: Moving rainbow wave across the seven logical columns. */
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

/* 6: Scanner / Cylon. One bright logical column bounces left-right. */
static void zmk_rgb_underglow_effect_scanner(void) {
    const uint8_t path_len = (EYELASH_LOGICAL_COLUMNS - 1U) * 2U;
    uint8_t pos = (uint8_t)((state.animation_step / 4U) % path_len);
    if (pos >= EYELASH_LOGICAL_COLUMNS) {
        pos = path_len - pos;
    }

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t distance = (uint8_t)abs((int)col - (int)pos);
        uint8_t pct = 0;

        if (distance == 0) {
            pct = 100;
        } else if (distance == 1) {
            pct = 45;
        } else if (distance == 2) {
            pct = 16;
        }

        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % (path_len * 4U);
}

/* 7: A ring starts at the center column and travels to both outer edges. */
static void zmk_rgb_underglow_effect_center_out(void) {
    const uint8_t center = EYELASH_LOGICAL_COLUMNS / 2U;
    const uint8_t ring = (uint8_t)((state.animation_step / 6U) % (center + 1U));

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t distance = (uint8_t)abs((int)col - (int)center);
        const uint8_t delta = (uint8_t)abs((int)distance - (int)ring);
        uint8_t pct = 0;

        if (delta == 0) {
            pct = 100;
        } else if (delta == 1) {
            pct = 28;
        } else if (delta == 2) {
            pct = 8;
        }

        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % ((center + 1U) * 6U);
}

/* 8: Meteor with a bright head and a three-column fading tail. */
static void zmk_rgb_underglow_effect_meteor(void) {
    const uint8_t head =
        (uint8_t)((state.animation_step / 4U) % EYELASH_LOGICAL_COLUMNS);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t trail =
            (uint8_t)((head + EYELASH_LOGICAL_COLUMNS - col) % EYELASH_LOGICAL_COLUMNS);
        uint8_t pct = 0;

        switch (trail) {
        case 0:
            pct = 100;
            break;
        case 1:
            pct = 60;
            break;
        case 2:
            pct = 32;
            break;
        case 3:
            pct = 12;
            break;
        default:
            pct = 0;
            break;
        }

        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % (EYELASH_LOGICAL_COLUMNS * 4U);
}

/* 9: A soft brightness wave travels through the columns. */
static void zmk_rgb_underglow_effect_pulse_wave(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint16_t phase =
            (state.animation_step + (200U * col) / EYELASH_LOGICAL_COLUMNS) % 200U;
        const uint8_t wave = eyelash_triangle_percent(phase, 200U);
        const uint8_t pct = 10U + (uint8_t)((wave * 90U) / 100U);

        pixels[i] = eyelash_effect_rgb(state.color.h, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed * 3U) % 200U;
}

/* 10: Alternating current hue / opposite hue, periodically swapping sides. */
static void zmk_rgb_underglow_effect_alternating(void) {
    const bool swap = ((state.animation_step / 10U) & 1U) != 0;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const bool opposite = ((col & 1U) != 0) ^ swap;
        const int hue = state.color.h + (opposite ? 180 : 0);

        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 20U;
}

/* 11: A compact gradient, shifted continuously by animation_step. */
static void zmk_rgb_underglow_effect_moving_gradient(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const int hue = state.color.h + col * 28 + state.animation_step;

        pixels[i] = eyelash_effect_rgb(hue, state.color.s, 100);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed * 2U) % HUE_MAX;
}

/* 12: Slow, soft neighboring hue and brightness changes. */
static void zmk_rgb_underglow_effect_aurora(void) {
    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint16_t phase = (state.animation_step + col * 23U) % 180U;
        const uint8_t wave = eyelash_triangle_percent(phase, 180U);
        const int hue_offset = ((int)wave - 50) / 2;
        const int hue = state.color.h + col * 8 + hue_offset;
        const uint8_t pct = 35U + (uint8_t)((wave * 65U) / 100U);

        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 180U;
}

/* 13: Deterministic sparkle. All three LEDs in a logical column sparkle together. */
static void zmk_rgb_underglow_effect_sparkle(void) {
    const uint32_t frame = state.animation_step / 3U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint32_t rnd = eyelash_hash32(frame * 0x9e3779b9U + col * 0x85ebca6bU);
        const bool sparkle = (rnd & 0x7U) < 2U;
        const uint8_t pct = sparkle ? 100U : (uint8_t)(10U + ((rnd >> 8) % 16U));
        const int hue = state.color.h + (int)((rnd >> 16) % 31U) - 15;

        pixels[i] = eyelash_effect_rgb(hue, state.color.s, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 60000U;
}

/* 14: Warm red/orange/yellow flame. Hue is intentionally independent of HUI/HUD. */
static void zmk_rgb_underglow_effect_fire(void) {
    const uint32_t frame = state.animation_step / 2U;

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint32_t rnd = eyelash_hash32(frame * 0x27d4eb2dU + col * 0x165667b1U);
        const int hue = (int)(rnd % 46U); /* red -> orange -> yellow */
        const uint8_t pct = (uint8_t)(40U + ((rnd >> 8) % 61U));

        pixels[i] = eyelash_effect_rgb(hue, 100, pct);
    }

    state.animation_step = (state.animation_step + state.animation_speed) % 60000U;
}

/* 15: Green moving head with a fading trail. */
static void zmk_rgb_underglow_effect_matrix(void) {
    const uint8_t head =
        (uint8_t)((state.animation_step / 4U) % EYELASH_LOGICAL_COLUMNS);

    for (int i = 0; i < STRIP_NUM_PIXELS; i++) {
        const uint8_t col = eyelash_logical_column(i);
        const uint8_t trail =
            (uint8_t)((head + EYELASH_LOGICAL_COLUMNS - col) % EYELASH_LOGICAL_COLUMNS);
        uint8_t pct = 0;

        switch (trail) {
        case 0:
            pct = 100;
            break;
        case 1:
            pct = 55;
            break;
        case 2:
            pct = 28;
            break;
        case 3:
            pct = 12;
            break;
        default:
            pct = 0;
            break;
        }

        pixels[i] = eyelash_effect_rgb(120, 100, pct);
    }

    state.animation_step =
        (state.animation_step + state.animation_speed) % (EYELASH_LOGICAL_COLUMNS * 4U);
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
    default:
        /* Never leave stale pixels on screen if settings somehow contain an invalid ID. */
        zmk_rgb_underglow_effect_solid();
        break;
    }
    int err = led_strip_update_rgb(led_strip, pixels, STRIP_NUM_PIXELS);
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
    }

    led_strip_update_rgb(led_strip, pixels, STRIP_NUM_PIXELS);
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
    return (state.current_effect + UNDERGLOW_EFFECT_NUMBER + direction) % UNDERGLOW_EFFECT_NUMBER;
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
