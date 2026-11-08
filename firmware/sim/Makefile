# bento-sim: the real UX and link in a native Mac window. `make` builds it; `make asan` builds the sanitized one for tests.
# `make sharp` builds bento-sim-sharp, which draws the same UX at twice the resolution with fonts generated at those
# sizes, because a 360-pixel panel shown on a Mac's screen is either tiny or soft. Tests use the other two: their pixels
# are the firmware's.
CC = clang
ROOT := $(abspath ..)
BUILD := build
# cJSON comes from ESP-IDF, as on the board: $(IDF_PATH) when ESP-IDF is loaded, or where its installer puts it.
IDF_PATH ?= $(HOME)/esp/esp-idf
CJSON_DIR := $(IDF_PATH)/components/json/cJSON
ifeq ($(wildcard $(CJSON_DIR)/cJSON.c),)
$(error No cJSON in $(CJSON_DIR): install ESP-IDF 5.5 (see the README), or set IDF_PATH)
endif

UX_SRC := $(wildcard $(ROOT)/ux/*.c $(ROOT)/ux/*/*.c)
LINK_SRC := $(wildcard $(ROOT)/link/*.c)
SIM_SRC := hal_sim.c demo.c board_select.c

WARN := -Wall -Wextra
COMMON := -std=gnu17 -I$(ROOT)/hal -I$(CJSON_DIR) -DBENTO_DEBUG_INTERFACE -g
# Everything that reads BOARD sees it writable, as board_select.c defines it; the boards' own tables stay constant.
READS_BOARD := -DBOARD_CONST=
OPT := -O2
FRAMEWORKS := -framework AppKit -framework CoreGraphics -framework Foundation

# Each variant is a folder of objects: the usual one, and one under AddressSanitizer and UBSan.
SAN := -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=undefined

.PHONY: all asan sharp test run clean
all: $(BUILD)/bento-sim
asan: $(BUILD)/bento-sim-asan
sharp: $(BUILD)/bento-sim-sharp

# make run BOARD=amoled-2.16 ARGS=--demo. (tools/bento sim <board> comes here.)
BOARD ?= knob-lcd-1.8
ARGS ?=
run: sharp
	$(BUILD)/bento-sim-sharp --board $(BOARD) $(ARGS)

test: all asan
	python3 tests/run_tests.py

define VARIANT
$(1)_OBJ := $$(patsubst $$(ROOT)/%.c,$$(BUILD)/$(1)/%.o,$$(UX_SRC) $$(LINK_SRC)) \
            $$(patsubst %.c,$$(BUILD)/$(1)/sim/%.o,$$(SIM_SRC)) \
            $$(BUILD)/$(1)/sim/main.o $$(BUILD)/$(1)/cJSON.o \
            $$(BUILD)/$(1)/board_knob.o $$(BUILD)/$(1)/board_amoled.o

$$(BUILD)/$(1)/%.o: $$(ROOT)/%.c
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $$(READS_BOARD) $(2) $$(WARN) -MMD -MP -c $$< -o $$@

$$(BUILD)/$(1)/sim/%.o: %.c
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $$(READS_BOARD) $(2) $$(WARN) -MMD -MP -c $$< -o $$@

$$(BUILD)/$(1)/sim/main.o: main.m sim.h
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $$(READS_BOARD) $(2) $$(WARN) -fobjc-arc -c $$< -o $$@

# Third-party, unchanged: no warnings asked of it.
$$(BUILD)/$(1)/cJSON.o: $$(CJSON_DIR)/cJSON.c
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $(2) -w -c $$< -o $$@

$$(BUILD)/$(1)/board_knob.o: $$(ROOT)/boards/knob-lcd-1.8/board_info.c $$(ROOT)/hal/hal.h
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $(2) $$(WARN) -DBOARD=BOARD_knob_lcd_1_8 -c $$< -o $$@

$$(BUILD)/$(1)/board_amoled.o: $$(ROOT)/boards/amoled-2.16/board_info.c $$(ROOT)/hal/hal.h
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON) $(2) $$(WARN) -DBOARD=BOARD_amoled_2_16 -c $$< -o $$@

-include $$($(1)_OBJ:.o=.d)
endef

$(eval $(call VARIANT,plain,$(OPT)))
$(eval $(call VARIANT,san,-O1 $(SAN)))
$(eval $(call VARIANT,sharp,$(OPT) -DSIM_RENDER=2 -DGLYPH_OFFSET_T=int16_t))

# The sharp build's fonts and icons replace the firmware's. They're generated here, not kept in the repo: about 13 MB of source.
TOOLS := $(abspath ../../tools)
sharp_OBJ := $(filter-out $(BUILD)/sharp/ux/assets/fonts.o $(BUILD)/sharp/ux/assets/icons.o,$(sharp_OBJ)) $(BUILD)/sharp/fonts_sharp.o $(BUILD)/sharp/icons_sharp.o

$(BUILD)/sharp/fonts_sharp.c: $(TOOLS)/gen_fonts.py
	@mkdir -p $(dir $@)
	$(TOOLS)/.venv/bin/python $(TOOLS)/gen_fonts.py --sharp $@ > /dev/null

$(BUILD)/sharp/fonts_sharp.o: $(BUILD)/sharp/fonts_sharp.c $(ROOT)/ux/assets/fonts.h
	$(CC) -std=gnu17 -I$(ROOT)/ux/assets -DGLYPH_OFFSET_T=int16_t -O0 -w -c $< -o $@

$(BUILD)/sharp/icons_sharp.c: $(TOOLS)/gen_icons.py $(ROOT)/../preview/bento.html
	@mkdir -p $(dir $@)
	$(TOOLS)/.venv/bin/python $(TOOLS)/gen_icons.py --sharp $@ > /dev/null 2>&1

$(BUILD)/sharp/icons_sharp.o: $(BUILD)/sharp/icons_sharp.c $(ROOT)/ux/assets/icons.h
	$(CC) -std=gnu17 -I$(ROOT)/ux/assets -O0 -w -c $< -o $@

$(BUILD)/bento-sim: $(plain_OBJ)
	$(CC) $(plain_OBJ) $(FRAMEWORKS) -lm -o $@

$(BUILD)/bento-sim-asan: $(san_OBJ)
	$(CC) $(SAN) $(san_OBJ) $(FRAMEWORKS) -lm -o $@

$(BUILD)/bento-sim-sharp: $(sharp_OBJ)
	$(CC) $(sharp_OBJ) $(FRAMEWORKS) -lm -o $@

clean:
	rm -rf $(BUILD)

# The protocol's test values. link.c's utf8_copy is copied out here so that this needs no HAL.
$(BUILD)/test_vectors: tests/test_vectors.c $(ROOT)/link/adpcm.c $(ROOT)/link/link.h
	@mkdir -p $(BUILD)
	$(CC) $(COMMON) $(SAN) -O1 $(WARN) -DVECTORS_ONLY tests/test_vectors.c tests/test_vectors_utf8.c $(ROOT)/link/adpcm.c -o $@
