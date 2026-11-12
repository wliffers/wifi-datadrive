idf_component_register(SRCS "app_main.c" "drive.c" "usb_dev.c" "ui.c"
                                "auth.c" "ota.c" "web_server.c" "web_static.c" "web_files.c" "web_system.c" "web_wifi.c"
                                "web_auth.c" "web_ota.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES board display pd_wifi applog
                                     esp_tinyusb fatfs wear_levelling vfs esp_partition sdmmc esp_driver_sdmmc
                                     nvs_flash esp_http_server esp_timer esp_system app_update mbedtls
                       EMBED_FILES "web/manifest.webmanifest" "web/icons/icon-180.png"
                                   "web/icons/icon-192.png" "web/icons/icon-512.png")

# Web UI: compressed at build time and served with "Content-Encoding: gzip".
idf_build_get_property(python PYTHON)
set(web_src ${COMPONENT_DIR}/web/index.html)
set(web_gz ${CMAKE_CURRENT_BINARY_DIR}/index.html.gz)
add_custom_command(OUTPUT ${web_gz}
                   COMMAND ${python} ${COMPONENT_DIR}/web/gzip_file.py ${web_src} ${web_gz}
                   DEPENDS ${web_src} ${COMPONENT_DIR}/web/gzip_file.py
                   VERBATIM)
add_custom_target(web_ui_gz DEPENDS ${web_gz})
target_add_binary_data(${COMPONENT_LIB} ${web_gz} BINARY DEPENDS web_ui_gz)

# Host-I/O gate (see drive.c): route TinyUSB's calls of these MSC callbacks through
# our __wrap_* functions, which call esp_tinyusb's implementation via __real_*.
# Only callbacks WITHOUT a weak default in TinyUSB can be wrapped this way.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_msc_read10_cb"
    "-Wl,--wrap=tud_msc_write10_cb"
    "-Wl,--wrap=tud_msc_test_unit_ready_cb")
