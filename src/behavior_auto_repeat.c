/*
 * SPDX-License-Identifier: MIT
 * Generic hold-to-repeat wrapper for ZMK behaviors.
 */
#define DT_DRV_COMPAT zmk_behavior_auto_repeat

#include <errno.h>
#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>

#include <drivers/behavior.h>
#include <zmk/behavior.h>
#include <zmk/keymap.h>

struct behavior_auto_repeat_config {
    uint32_t delay_ms;
    uint32_t interval_ms;
    struct zmk_behavior_binding target;
};

struct behavior_auto_repeat_data {
    const struct device *dev;
    struct k_work_delayable work;
    struct zmk_behavior_binding_event event;
    atomic_t active;
};

/* Execute the configured behavior as a short press, without changing its action. */
static void invoke_target(const struct behavior_auto_repeat_config *config,
                          struct zmk_behavior_binding_event event) {
    event.timestamp = k_uptime_get();
    (void)zmk_behavior_invoke_binding(&config->target, event, true);
    event.timestamp = k_uptime_get();
    (void)zmk_behavior_invoke_binding(&config->target, event, false);
}

static void auto_repeat_work_handler(struct k_work *work) {
    struct k_work_delayable *delayable = k_work_delayable_from_work(work);
    struct behavior_auto_repeat_data *data =
        CONTAINER_OF(delayable, struct behavior_auto_repeat_data, work);
    const struct behavior_auto_repeat_config *config = data->dev->config;

    if (!atomic_get(&data->active)) {
        return;
    }

    invoke_target(config, data->event);

    if (atomic_get(&data->active)) {
        (void)k_work_reschedule(&data->work, K_MSEC(config->interval_ms));
    }
}

static int behavior_auto_repeat_init(const struct device *dev) {
    struct behavior_auto_repeat_data *data = dev->data;
    data->dev = dev;
    atomic_set(&data->active, 0);
    k_work_init_delayable(&data->work, auto_repeat_work_handler);
    return 0;
}

static int on_auto_repeat_binding_pressed(struct zmk_behavior_binding *binding,
                                          struct zmk_behavior_binding_event event) {
    const struct device *dev = zmk_behavior_get_binding(binding->behavior_dev);
    if (dev == NULL) {
        return -ENODEV;
    }

    struct behavior_auto_repeat_data *data = dev->data;
    const struct behavior_auto_repeat_config *config = dev->config;

    atomic_set(&data->active, 0);
    (void)k_work_cancel_delayable(&data->work);

    data->event = event;
    atomic_set(&data->active, 1);

    /* Preserve normal tap behavior: execute immediately on key-down. */
    invoke_target(config, event);
    (void)k_work_reschedule(&data->work, K_MSEC(config->delay_ms));

    return ZMK_BEHAVIOR_OPAQUE;
}

static int on_auto_repeat_binding_released(struct zmk_behavior_binding *binding,
                                           struct zmk_behavior_binding_event event) {
    ARG_UNUSED(event);

    const struct device *dev = zmk_behavior_get_binding(binding->behavior_dev);
    if (dev == NULL) {
        return ZMK_BEHAVIOR_OPAQUE;
    }

    struct behavior_auto_repeat_data *data = dev->data;
    atomic_set(&data->active, 0);
    (void)k_work_cancel_delayable(&data->work);

    return ZMK_BEHAVIOR_OPAQUE;
}

static const struct behavior_driver_api behavior_auto_repeat_driver_api = {
    .locality = BEHAVIOR_LOCALITY_GLOBAL,
    .binding_pressed = on_auto_repeat_binding_pressed,
    .binding_released = on_auto_repeat_binding_released,
#if IS_ENABLED(CONFIG_ZMK_BEHAVIOR_METADATA)
    .get_parameter_metadata = zmk_behavior_get_empty_param_metadata,
#endif
};

#define AUTO_REPEAT_INST(inst)                                                                  \
    static struct behavior_auto_repeat_data behavior_auto_repeat_data_##inst;                   \
    static const struct behavior_auto_repeat_config behavior_auto_repeat_config_##inst = {      \
        .delay_ms = DT_INST_PROP(inst, delay_ms),                                                \
        .interval_ms = DT_INST_PROP(inst, interval_ms),                                          \
        .target = ZMK_KEYMAP_EXTRACT_BINDING(0, DT_DRV_INST(inst)),                              \
    };                                                                                          \
    BEHAVIOR_DT_INST_DEFINE(inst, behavior_auto_repeat_init, NULL,                              \
                            &behavior_auto_repeat_data_##inst,                                 \
                            &behavior_auto_repeat_config_##inst, POST_KERNEL,                   \
                            CONFIG_KERNEL_INIT_PRIORITY_DEFAULT,                               \
                            &behavior_auto_repeat_driver_api);

DT_INST_FOREACH_STATUS_OKAY(AUTO_REPEAT_INST)
