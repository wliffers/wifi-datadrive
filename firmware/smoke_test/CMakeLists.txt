# Phase 0 hardware smoke test: LCD, RGB LED, BOOT button, flash size, Wi-Fi.
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS ../components)
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(printdrive_smoke)
