# Scripts code used in uConsole os image

## uconsole-4g-cm4

Bash script to PowerOn/PowerOff 4G ext module in uConsole.

## uconsole-4g-cm5

Power control for the CM5 4G extension, using `gpioset` from **libgpiod
2.x**. Install your distribution's GPIO command-line tools package (`gpiod`
on Debian-based systems), and check `gpioset --version`. Version 1.x is not
compatible. Run with permission to access `/dev/gpiochip0`:

```sh
sudo ./uconsole-4g-cm5 enable
sudo ./uconsole-4g-cm5 disable
```

This script implements the CM5 GPIO sequence reported in
[issue #44](https://github.com/clockworkpi/uConsole/issues/44). The sequence
still needs verification on the target hardware after changes. GPIO levels
after `gpioset` exits depend on the platform; these commands are specific to
the uConsole CM5 wiring and must not be treated as a generic GPIO controller.

Configure cellular connections separately with your preferred network tools.
The script works without systemd or ModemManager. It does not restart services,
select an APN, request a SIM PIN, or change routes. If using ModemManager,
`mmcli -L` checks detection after power-on. The former `status`, `connect`,
`disconnect`, `unlock`, `diagnose`, and `reset` commands have been removed;
use your modem/network manager for those operations.

## audio_3.5_patch.py

Python script to switch between 3.5 audio jack and speaker on uConsole when 3.5 audio jack plugged or not.
