# Bento firmware. The board is a folder under boards/: -DBOARD=<folder>. tools/bento passes it, and builds
# each board in its own build-<board> folder (build-<board>-debug with --debug), with its own sdkconfig inside.
cmake_minimum_required(VERSION 3.16)

if(NOT BOARD)
    file(GLOB known_boards RELATIVE ${CMAKE_CURRENT_LIST_DIR}/boards ${CMAKE_CURRENT_LIST_DIR}/boards/*)
    list(REMOVE_ITEM known_boards _template)
    message(FATAL_ERROR "Which board? Use tools/bento build <board>, or idf.py -B build-<board> -DBOARD=<board> build. "
                        "Boards: ${known_boards}")
endif()
if(NOT EXISTS ${CMAKE_CURRENT_LIST_DIR}/boards/${BOARD}/board_info.c)
    message(FATAL_ERROR "No board called \"${BOARD}\" in firmware/boards/")
endif()
get_filename_component(build_folder ${CMAKE_BINARY_DIR} NAME)
if(NOT build_folder STREQUAL "build-${BOARD}" AND NOT build_folder STREQUAL "build-${BOARD}-debug")
    message(FATAL_ERROR "Build ${BOARD} in build-${BOARD} (or build-${BOARD}-debug), not ${build_folder}, so two "
                        "boards' builds never mix.")
endif()

# Component CMakeLists files and the component manager's rules run in other processes, so they read the board here.
set(ENV{BENTO_BOARD} ${BOARD})

set(SDKCONFIG ${CMAKE_BINARY_DIR}/sdkconfig)
set(SDKCONFIG_DEFAULTS sdkconfig.defaults boards/${BOARD}/sdkconfig.board)
set(EXTRA_COMPONENT_DIRS drivers)
set(PROJECT_VER "0.2.0") # as BENTO_FW_VERSION in ux/app.h says

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# Build only what main needs: the board's drivers (boards/<board>/board.cmake) and what they use. Every driver's
# downloads stay in managed_components/, so switching boards doesn't delete and fetch them again.
idf_build_set_property(MINIMAL_BUILD ON)

project(bento)
