idf_component_register(SRCS "app_main.c" "drive.c" "usb_dev.c" "web_server.c" "ui.c"
                       INCLUDE_DIRS "."
                       EMBED_TXTFILES "web/index.html"
                       PRIV_REQUIRES board display pd_wifi applog
                                     esp_tinyusb fatfs wear_levelling vfs esp_partition
                                     nvs_flash esp_http_server esp_timer esp_system app_update)

# Host-I/O gate (see drive.c): route TinyUSB's calls of these MSC callbacks through
# our __wrap_* functions, which call esp_tinyusb's implementation via __real_*.
# Only callbacks WITHOUT a weak default in TinyUSB can be wrapped this way.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_msc_read10_cb"
    "-Wl,--wrap=tud_msc_write10_cb"
    "-Wl,--wrap=tud_msc_test_unit_ready_cb")
