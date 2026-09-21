ARDUINO_CLI ?= arduino-cli
FQBN ?= stm32duino:STM32F1:genericSTM32F103R:device_variant=STM32F103RB,upload_method=DFUUploadMethod,cpu_speed=speed_48mhz,opt=osstd
BUILD_DIR ?= $(CURDIR)/build
CFLAGS ?= -O2 -Wall -Wextra

.PHONY: all firmware flash-tool flash-bundle check clean
all: firmware flash-tool

firmware:
	$(ARDUINO_CLI) compile --fqbn '$(FQBN)' --output-dir '$(BUILD_DIR)/firmware' Code/uconsole_keyboard

flash-tool:
	mkdir -p '$(BUILD_DIR)'
	$(CC) $(CPPFLAGS) $(CFLAGS) Bin/uconsole_keyboard_flash/upload-reset/upload-reset.c $(LDFLAGS) $(LDLIBS) -o '$(BUILD_DIR)/upload-reset.elf'

flash-bundle: firmware flash-tool
	python3 tools/package_flash.py '$(BUILD_DIR)'

check:
	python3 -m unittest discover -s tests -v
	shellcheck Code/scripts/uconsole-4g-cm5 Bin/uconsole_keyboard_flash/maple_upload Bin/uconsole_keyboard_flash/flash.sh

clean:
	rm -rf -- '$(BUILD_DIR)'
