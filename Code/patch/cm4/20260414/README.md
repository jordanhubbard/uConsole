# CM4 / CM5 kernel for the April 2026 image

Source: <https://github.com/cuu/ClockworkPi-linux>, branch `rpi-6.12.y`,
commit `03436f4117533693d608b10eb4b3965987230b68` (Linux 6.12.62).
Use that exact revision, then apply `0001-remove-unused-panel-locals.patch`
from this directory in the kernel tree. The patch removes five unused local
variables without changing panel initialization or display behavior.

## Build

Select one profile and a separate output directory for each:

| Core | Defconfig | Output directory | Boot image name |
| --- | --- | --- | --- |
| CM4 | `bcm2711_defconfig` | `build-cm4` | `kernel8.img` |
| CM5 | `bcm2712_defconfig` | `build-cm5` | `kernel_2712.img` |

The CM5 configuration uses 16 KiB pages. Keep each image, configuration and
module directory together. These builds have been verified on an ARM64 Linux
host; boot and peripheral operation still require the target hardware.

From the kernel source directory (CM4 example; use the table for CM5):

```sh
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
KERNEL_BUILD="$PWD/build-cm4"
BOOT_IMAGE=kernel8.img
DEFCONFIG=bcm2711_defconfig

mkdir -p "$KERNEL_BUILD/tmp"
export TMPDIR="$KERNEL_BUILD/tmp"
make O="$KERNEL_BUILD" "$DEFCONFIG"
# Optional customization: make O="$KERNEL_BUILD" menuconfig
make O="$KERNEL_BUILD" -j8 Image.gz headers modules dtbs
```

`TMPDIR` keeps compiler scratch files on the build filesystem, including when
that filesystem is RAM-backed or on a separate disk. Source and object files
alone being on another filesystem does not stop GCC from filling `/tmp`.

## Stage a kernel archive

Continue in the same shell after a successful build. This stages files without
installing anything into the host's `/boot` or `/lib/modules`:

```sh
VERSION=$(make -s O="$KERNEL_BUILD" kernelrelease)
KERNEL_STAGE="$PWD/install-$VERSION"
mkdir -p "$KERNEL_STAGE/boot/firmware/overlays"
make O="$KERNEL_BUILD" INSTALL_MOD_PATH="$KERNEL_STAGE" modules_install

cp "$KERNEL_BUILD/arch/$ARCH/boot/Image.gz" "$KERNEL_STAGE/boot/firmware/$BOOT_IMAGE"
cp "$KERNEL_BUILD/arch/$ARCH/boot/dts/broadcom/"*.dtb "$KERNEL_STAGE/boot/firmware/"
cp "$KERNEL_BUILD/arch/$ARCH/boot/dts/overlays/"*.dtbo "$KERNEL_STAGE/boot/firmware/overlays/"
cp "arch/$ARCH/boot/dts/overlays/README" "$KERNEL_STAGE/boot/firmware/overlays/"
cp "$KERNEL_BUILD/.config" "$KERNEL_STAGE/boot/config-$VERSION"
cp "$KERNEL_BUILD/System.map" "$KERNEL_STAGE/boot/System.map-$VERSION"

# The build symlink points to this machine's source/output and is not portable.
tar --exclude="./lib/modules/$VERSION/build" \
    -czf "install-$VERSION.tar.gz" -C "$KERNEL_STAGE" .
```

This produces a tar archive, not a Debian package. `make headers_install`
exports userspace API headers; those alone are not a complete kernel header
package for building external modules. Use the kernel's supported packaging
workflow if a Debian image/header package is needed.

## Check compiled display overlays

Use Raspberry Pi's [dtmerge utility](https://github.com/raspberrypi/utils/tree/master/dtmerge)
for overlays with Raspberry Pi parameters and dormant fragments. Generic
`fdtoverlay` can reject the combined display overlays even though `dtmerge`
applies them successfully.

From the uConsole repository, after building the kernel and `dtmerge`:

```sh
python3 tools/verify_display_overlays.py \
  --dtmerge /path/to/dtmerge \
  --dts-dir /path/to/kernel/build-cm4/arch/arm64/boot/dts \
  --output build/kernel-cm4 --model cm4
```

For CM5, select its output directory and `--model cm5`. The check combines
the uConsole and VC4 overlays with `cma-384`, then checks panel orientation,
enabled display nodes, battery property and CMA size. It emits the merged DTB
and JSON evidence. This is structural validation, not a hardware boot test.

# config.txt

config.txt needs some modifications for cm4/cm5 to boot.
```
[pi4]         
#dtoverlay=clockworkpi-devterm      
dtoverlay=clockworkpi-uconsole   
dtoverlay=vc4-kms-v3d-pi4,cma-384  
enable_uart=1

[pi5]
#dtoverlay=clockworkpi-devterm-cm5
dtoverlay=clockworkpi-uconsole-cm5
dtoverlay=vc4-kms-v3d-pi5,cma-384
dtparam=uart0
dtparam=pciex1
dtparam=pciex1_gen=3

[all]                                                                                                                                                   
ignore_lcd=1 
max_framebuffers=2
disable_overscan=1
dtparam=audio=on
dtoverlay=audremap,pins_12_13
dtoverlay=dwc2,dr_mode=host
dtparam=ant2
dtparam=spi=on
dtoverlay=spi0-0cs
gpio=10=ip,np
gpio=9=op,dh
```

