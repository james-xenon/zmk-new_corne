#define DT_DRV_COMPAT james_toggle_layer13

#include <zephyr/device.h>
#include <zephyr/logging/log.h>
#include <drivers/behavior.h>
#include <zmk/behavior.h>
#include <zmk/keymap.h>
#include <zmk_ws2812_widget/widget.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

static int on_keymap_binding_pressed(struct zmk_behavior_binding *binding,
                                     struct zmk_behavior_binding_event event) {
    ARG_UNUSED(binding);
    ARG_UNUSED(event);

    /* Toggle layer 13 */
    int rc = zmk_keymap_layer_toggle(13);

    /* If layer now active -> set persistent ON color, else set persistent OFF color */
    if (rc >= 0) {
        bool active = zmk_keymap_layer_active(13);
        if (active) {
            ws2812_set_persistent_layer_color(13, CONFIG_WS2812_WIDGET_LAYER_COLOR_ON, 0, WS2812_NUM_PIXELS);
        } else {
            /* set OFF color (red) */
            ws2812_set_persistent_layer_color(13, CONFIG_WS2812_WIDGET_LAYER_COLOR_OFF, 0, WS2812_NUM_PIXELS);
        }
    }

    return ZMK_BEHAVIOR_OPAQUE;
}

static int on_keymap_binding_released(struct zmk_behavior_binding *binding,
                                      struct zmk_behavior_binding_event event) {
    ARG_UNUSED(binding);
    ARG_UNUSED(event);
    return ZMK_BEHAVIOR_OPAQUE;
}

static const struct behavior_driver_api behavior_toggle_layer13_driver_api = {
    .locality = BEHAVIOR_LOCALITY_LOCAL,
    .binding_pressed = on_keymap_binding_pressed,
    .binding_released = on_keymap_binding_released,
};

#define TOGGLE13_INST(n) \
    BEHAVIOR_DT_INST_DEFINE(n, NULL, NULL, NULL, NULL, POST_KERNEL, \
                            CONFIG_KERNEL_INIT_PRIORITY_DEFAULT, &behavior_toggle_layer13_driver_api);

DT_INST_FOREACH_STATUS_OKAY(TOGGLE13_INST)
