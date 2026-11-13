# WiFi DataDrive main firmware (project folder keeps its original name).
cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.6.0")
set(EXTRA_COMPONENT_DIRS ../components)
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(printdrive)
