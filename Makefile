ARDUINO_CLI ?= arduino-cli
FQBN ?= stm32duino:STM32F1:genericSTM32F103R:device_variant=STM32F103RB,upload_method=DFUUploadMethod,cpu_speed=speed_48mhz,opt=osstd
BUILD_DIR ?= $(CURDIR)/build
CFLAGS ?= -O2 -Wall -Wextra

.PHONY: all firmware flash-tool flash-bundle modem-flash-tool install-modem-updater check check-gui clean
all: firmware flash-tool modem-flash-tool

firmware:
	$(ARDUINO_CLI) compile --fqbn '$(FQBN)' --output-dir '$(BUILD_DIR)/firmware' Code/uconsole_keyboard

flash-tool:
	mkdir -p '$(BUILD_DIR)'
	$(CC) $(CPPFLAGS) $(CFLAGS) Bin/uconsole_keyboard_flash/upload-reset/upload-reset.c $(LDFLAGS) $(LDLIBS) -o '$(BUILD_DIR)/upload-reset.elf'

flash-bundle: firmware flash-tool
	python3 tools/package_flash.py '$(BUILD_DIR)'

modem-flash-tool:
	mkdir -p '$(BUILD_DIR)/modem-fastboot'
	tar -xzf Bin/4G/LE20B04SIM7600G22_cpi_arm64.tar.gz -C '$(BUILD_DIR)/modem-fastboot' --strip-components=2 LE20B04SIM7600G22_cpi_arm64/fastboot
	patch -d '$(BUILD_DIR)/modem-fastboot' -p1 < Code/patch/modem/fastboot-path-bounds.patch
	$(MAKE) -B -C '$(BUILD_DIR)/modem-fastboot' CC='$(CC)' all

check:
	python3 -m unittest discover -s tests -v
	shellcheck Code/scripts/uconsole-4g-cm5 Bin/uconsole_keyboard_flash/maple_upload Bin/uconsole_keyboard_flash/flash.sh

check-gui:
	xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -p 'test_modem_gui.py' -v

# Build as the normal user before running this installation target with sudo.
install-modem-updater:
	test -x '$(BUILD_DIR)/modem-fastboot/bin/fastboot'
	install -d '$(DESTDIR)/usr/lib/uconsole-modem' '$(DESTDIR)/usr/bin' '$(DESTDIR)/usr/share/applications'
	install -m 755 Code/scripts/uconsole-modem-flash.py Code/scripts/uconsole-modem-gui.py '$(DESTDIR)/usr/lib/uconsole-modem/'
	install -m 644 Code/scripts/modem-firmware-manifest.json '$(DESTDIR)/usr/lib/uconsole-modem/'
	install -m 755 '$(BUILD_DIR)/modem-fastboot/bin/fastboot' '$(DESTDIR)/usr/lib/uconsole-modem/fastboot'
	ln -sfn ../lib/uconsole-modem/uconsole-modem-gui.py '$(DESTDIR)/usr/bin/uconsole-modem-updater'
	install -m 644 Code/scripts/desktop/uconsole-modem-updater.desktop '$(DESTDIR)/usr/share/applications/'

clean:
	rm -rf -- '$(BUILD_DIR)'
