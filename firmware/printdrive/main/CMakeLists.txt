idf_component_register(SRCS "app_main.c" "drive.c" "usb_dev.c" "ui.c" "anim.c"
                                "auth.c" "ota.c" "web_server.c" "web_static.c" "web_files.c" "web_system.c" "web_wifi.c" "web_crypt.c"
                                "web_auth.c" "web_ota.c" "tls.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES board display pd_wifi applog pd_crypt
                                     esp_tinyusb fatfs wear_levelling vfs esp_partition sdmmc esp_driver_sdmmc
                                     nvs_flash esp_http_server esp_https_server esp_timer esp_system app_update mbedtls
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
    "-Wl,--wrap=tud_msc_test_unit_ready_cb"
    "-Wl,--wrap=tud_msc_capacity_cb")

# Drive encryption (see usb_dev.c): esp_tinyusb reaches the medium through a storage_medium_t
# table returned by these two functions (called from tinyusb_msc.c, defined in storage_*.c, so
# --wrap applies). Our wrappers hand out a table that routes through pd_crypt. The type lives
# in esp_tinyusb's private headers (version pinned in dependencies.lock).
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=storage_sdmmc_open_medium"
    "-Wl,--wrap=storage_spiflash_open_medium")
idf_component_get_property(esp_tinyusb_dir espressif__esp_tinyusb COMPONENT_DIR)
target_include_directories(${COMPONENT_LIB} PRIVATE "${esp_tinyusb_dir}/include_private")
