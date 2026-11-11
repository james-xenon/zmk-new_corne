EYELASH CORNE — POWER FIX V3

Причина падения предыдущей сборки:
- V2 patch_firmware_power.py искал слишком точный текстовый anchor в widget.c.
- GitHub Actions получил zmk-ws2812-driver commit a8674a9, но формат wake-блока
  не совпал с жёстким anchor, поэтому patcher корректно остановился ДО изменения файлов.
- V3 использует whitespace-tolerant проверки и одновременно pin-ит виджет на a8674a9.

Что меняется:
1. Battery VDDH patch не используется.
2. CONFIG_ZMK_BATTERY_REPORT_INTERVAL=60.
3. CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=7 отсутствует; используется штатный ZMK default.
4. Левый энкодер:
   - qdec0 включён только в eyelash_corne_left.overlay;
   - patch_qdec_sample_fifo.py и patch_zmk_sensor_fifo.py применяются только к LEFT build;
   - исправление «каждый второй щелчок» сохраняется.
5. Правая половина:
   - qdec0 остаётся disabled;
   - workflow после сборки проверяет отсутствие CONFIG_QDEC_NRFX=y;
   - 5-позиционный джойстик НЕ затрагивается: он является частью обычной GPIO matrix.
6. patch_firmware_power.py V3:
   - SAADC low-power fix сохранён;
   - SPIM3 Anomaly 195 fix сохранён;
   - после чёрного WS2812 idle frame EXT_POWER отключается;
   - перед wake redraw питание включается только если нужен underglow/static indicator;
   - после boot при реально выключенном underglow и отсутствии static indication rail отключается.
7. zmk-ws2812-driver зафиксирован на commit a8674a9 в config/west.yml.
8. CONFIG_ZMK_BACKLIGHT=y и CONFIG_ZMK_BACKLIGHT_BRT_START=100 СОХРАНЕНЫ как раньше.
   Это отдельный PWM P1.13, а не красные battery flashes. Его специально не меняем
   в этом тесте, чтобы не смешивать функциональное изменение с исправлением расхода.

Красные предупреждения батареи по-прежнему делает WS2812_WIDGET:
CONFIG_WS2812_WIDGET_BATTERY_REMINDER_COLOR=0xFF0000
CONFIG_WS2812_WIDGET_BATTERY_REMINDER_REPEAT_COUNT=3

Правый 4-way + click джойстик:
- это позиции GPIO key matrix;
- QDEC нужен только вращательному энкодеру слева;
- отключение qdec0 справа на джойстик не влияет.

Дополнительно workflow теперь НЕ применяет RGB/power source patches к settings_reset builds.
Они нужны только eyelash_corne_left/right, поэтому reset firmware собирается независимо.

Удалить из основного репозитория, если ещё остались:
- patches/patch_battery_vddh.py
- patches/patch_battery_vddh.py.before_anchor_fix.bak
- docs/BATTERY_FIX.md
- patches/__pycache__/
- patches/rgb_underglow.c.before_palette_update.bak
- patches/rgb_underglow.c.updated
Основной patches/rgb_underglow.c НЕ удалять.

Не удалять:
- patches/patch_qdec_sample_fifo.py
- patches/patch_zmk_sensor_fifo.py
- patches/patch_firmware_power.py
- patches/rgb_underglow.c

После замены файлов:
1. Commit + push.
2. Дождаться успешного GitHub Actions build.
3. Для чистого теста прошить settings_reset на обе половины, затем соответствующую firmware.
4. Полностью зарядить обе половины и сравнивать разряд при одинаковом использовании.
5. CONFIG_BT_CTLR_TX_PWR_PLUS_8=y пока оставлен без изменения; если асимметрия останется,
   это следующий отдельный тест.
6. PWM backlight P1.13 тоже пока сохранён. Если общий расход останется высоким, его нужно
   тестировать отдельным изменением, потому что он не связан с battery blink widget.
