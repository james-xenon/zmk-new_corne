ИСПРАВЛЕНИЯ ДЛЯ EYELASH CORNE

Что исправлено:
1. Battery VDDH patch больше не нужен и не вызывается.
2. CONFIG_ZMK_BATTERY_REPORT_INTERVAL = 60 секунд.
3. CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=7 отсутствует; используется штатное значение ZMK.
4. QDEC/SAMPLERDY-патч сохранён для энкодера, но применяется ТОЛЬКО к левой половине.
5. В общем devicetree qdec0 теперь disabled; left.overlay включает его, right.overlay явно оставляет disabled.
6. В workflow добавлена проверка, что CONFIG_QDEC_NRFX не попал в правую сборку.
7. patch_firmware_power.py:
   - SAADC fix сохранён;
   - SPIM3 Anomaly 195 fix сохранён;
   - WS2812 idle-power обновлён до V2:
     * после чёрного idle-кадра EXT_POWER отключается;
     * при пробуждении питание включается ДО восстановления underglow/static LEDs;
     * если свет не нужен, питание остаётся выключенным;
     * после загрузки при выключенном underglow и отсутствии static indication RGB rail отключается.
8. CONFIG_ZMK_BACKLIGHT=n. Это отдельный PWM-канал P1.13 и он НЕ отвечает за три красных
   вспышки батареи. Красные вспышки делает WS2812_WIDGET:
   CONFIG_WS2812_WIDGET_BATTERY_REMINDER_COLOR=0xFF0000
   CONFIG_WS2812_WIDGET_BATTERY_REMINDER_REPEAT_COUNT=3

Удалить из репозитория, если ещё остались:
- patches/patch_battery_vddh.py
- patches/patch_battery_vddh.py.before_anchor_fix.bak
- docs/BATTERY_FIX.md
- patches/__pycache__/
- patches/rgb_underglow.c.before_palette_update.bak
- patches/rgb_underglow.c.updated
(последние два — только резервные/неиспользуемые копии; основной patches/rgb_underglow.c НЕ удалять)

Что НЕ менять на первом тесте:
- CONFIG_BT_CTLR_TX_PWR_PLUS_8=y оставить, чтобы не смешивать несколько BLE-изменений.
- patches/patch_qdec_sample_fifo.py оставить: он нужен твоему левому энкодеру.
- patches/patch_zmk_sensor_fifo.py оставить.
- patches/rgb_underglow.c оставить.

После замены файлов:
1. Закоммитить/запушить.
2. Убедиться, что GitHub Actions собирается без ошибок.
3. Для чистого теста:
   a) flash settings_reset_right на правую;
   b) flash eyelash_corne_right на правую;
   c) flash settings_reset_left на левую;
   d) flash eyelash_corne_studio_left на левую;
   e) выключить обе половины примерно на 5 секунд, затем включить.
4. При необходимости удалить старое Bluetooth-сопряжение на компьютере и подключить заново.
5. Сравнивать разряд после полного заряда обеих половин при одинаковом использовании.

Почему QDEC был особенно важен:
- Zephyr включает QDEC_NRFX автоматически, если qdec node имеет status="okay".
- раньше qdec0="okay" находился в общем eyelash_corne.dtsi, поэтому он был включён и справа;
- workflow также накладывал SAMPLERDY patch на все matrix builds;
- твой patch использует SAMPLEPER=2048us и SAMPLERDY interrupts, то есть QDEC генерирует
  события с периодом около 2.048 ms (~488 Hz), даже хотя физического энкодера справа нет.
После исправления эта логика остаётся только на левой половине.
