# Kernel for uConsole_CM4_v3.1_64bit.img 
Kernel source tree:  
`https://github.com/cuu/ClockworkPi-linux`   branch rpi-6.12.y  
Commit hash:  
`03436f4117533693d608b10eb4b3965987230b68`  

# How to compile the kernel

```
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export DTS_SUBDIR=broadcom
export IMAGE=Image.gz
export KERNEL=kernel8


make O=build bcm2711_defconfig

make O=build menuconfig

make O=build -j 2 $IMAGE headers modules dtbs V=1

mkdir -p install/boot/overlays
make O=build INSTALL_MOD_PATH=install modules_install
cp build/arch/$ARCH/boot/dts/$DTS_SUBDIR/*.dtb install/boot/
cp build/arch/$ARCH/boot/dts/overlays/*.dtb* install/boot/overlays/
cp arch/${ARCH}/boot/dts/overlays/README install/boot/overlays/
cp build/arch/$ARCH/boot/$IMAGE install/boot/kernel8.img

rm -rf install/lib
mv build/install/lib/ install

rm -rf install.tar.gz
tar zcvf install.tar.gz install

```

## How to package kernel files to a deb file  

Use the files produced above 

```
VERSION=$(make O=build kernelrelease | sed -n '2p')
DEB_FOLDER=uconsole-kernel-cm4-rpi_$VERSION


mkdir -p $DEB_FOLDER/boot/firmware/overlays
mkdir -p $DEB_FOLDER/usr/src

make O=build INSTALL_MOD_PATH=install modules_install
make O=build INSTALL_HDR_PATH=install headers_install

cp build/arch/$ARCH/boot/dts/$DTS_SUBDIR/*.dtb $DEB_FOLDER/boot/firmware
cp build/arch/$ARCH/boot/dts/overlays/*.dtb* $DEB_FOLDER/boot/firmware/overlays/
cp arch/${ARCH}/boot/dts/overlays/README $DEB_FOLDER/boot/firmware/overlays/
cp build/arch/$ARCH/boot/$IMAGE $DEB_FOLDER/boot/firmware/kernel8.img

rm -rf $DEB_FOLDER/lib

cp -rf build/install/lib/ $DEB_FOLDER

cp build/.config  $DEB_FOLDER/boot/config-${VERSION}
cp build/System.map $DEB_FOLDER/boot/System.map-${VERSION}
cp build/arch/$ARCH/boot/$IMAGE $DEB_FOLDER/boot/vmlinuz-${VERSION}

cp -rf build/install/include/  $DEB_FOLDER/usr/src/linux-headers-${VERSION}

rm -rf ${DEB_FOLDER}.tar.gz
tar zcvf ${DEB_FOLDER}.tar.gz  ${DEB_FOLDER}
```
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


