# The application: the UX and link when they exist, the shared platform code, and this board's facts.
# Drivers are components in ../drivers, and this board's list is in boards/<board>/board.cmake.
set(firmware ${CMAKE_CURRENT_LIST_DIR}/..)
set(board_dir ${firmware}/boards/$ENV{BENTO_BOARD})
include(${board_dir}/board.cmake)

set(requires driver_api ${BOARD_DRIVERS} bt nvs_flash esp_timer esp_psram esp_driver_usb_serial_jtag json mbedtls)

if(CMAKE_BUILD_EARLY_EXPANSION)
    # This pass only asks what the component requires.
    idf_component_register(PRIV_REQUIRES ${requires})
    return()
endif()

file(GLOB_RECURSE ux_srcs CONFIGURE_DEPENDS ${firmware}/ux/*.c)
file(GLOB_RECURSE link_srcs CONFIGURE_DEPENDS ${firmware}/link/*.c)

set(srcs
    ${firmware}/platform/esp32/main.c
    ${firmware}/platform/esp32/hal_core.c
    ${firmware}/platform/esp32/hal_io.c
    ${firmware}/platform/esp32/hal_feedback.c
    ${firmware}/platform/esp32/hal_store.c
    ${firmware}/platform/esp32/hal_link.c
    ${firmware}/platform/esp32/hal_debug.c
    ${firmware}/platform/esp32/hw_test.c
    ${board_dir}/board.c
    ${board_dir}/board_info.c)

# Warn more in the hardware layer's own code than in anything the component manager brings in.
set_source_files_properties(${srcs} PROPERTIES COMPILE_OPTIONS "-Wextra;-Wno-unused-parameter;-Wno-missing-field-initializers")

# The UX and the link, or, only when asked, the hardware test instead (CONFIG_BENTO_HW_TEST, or tools/bento build
# <board> --hw-test, which sets BENTO_FORCE_HW_TEST).
set(run_hw_test OFF)
if(CONFIG_BENTO_HW_TEST OR BENTO_FORCE_HW_TEST)
    set(run_hw_test ON)
else()
    list(APPEND srcs ${ux_srcs} ${link_srcs})
endif()

set(includes ${firmware} ${firmware}/hal ${firmware}/platform/esp32 ${board_dir})
foreach(tree ux link)
    if(IS_DIRECTORY ${firmware}/${tree})
        list(APPEND includes ${firmware}/${tree})
        file(GLOB subdirs ${firmware}/${tree}/*)
        foreach(subdir ${subdirs})
            if(IS_DIRECTORY ${subdir})
                list(APPEND includes ${subdir})
            endif()
        endforeach()
    endif()
endforeach()

idf_component_register(SRCS ${srcs} INCLUDE_DIRS ${includes} PRIV_REQUIRES ${requires})

if(run_hw_test)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE BENTO_RUN_HW_TEST=1)
endif()
# The debug interface: off unless the sdkconfig turns it on, or tools/bento build|flash --debug sets BENTO_FORCE_DEBUG
# (in a build folder of its own, firmware/build-<board>-debug).
if(CONFIG_BENTO_DEBUG_INTERFACE OR BENTO_FORCE_DEBUG)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE BENTO_DEBUG_INTERFACE=1)
endif()
