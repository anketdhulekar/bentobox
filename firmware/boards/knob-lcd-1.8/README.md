# knob-lcd-1.8

[Waveshare ESP32-S3-Knob-Touch-LCD-1.8](https://www.waveshare.com/wiki/ESP32-S3-Knob-Touch-LCD-1.8): a 360 × 360 round LCD (ST77916) with a rotary knob, a CST816 touch controller, a DRV2605 vibration motor and a PDM microphone, on an ESP32-S3R8 with 16 MB flash.

## USB

The single USB-C socket reaches one of two chips, depending on which way the plug goes. You want the ESP32-S3, which shows up as `/dev/cu.usbmodem*`, Espressif USB-Serial/JTAG. If you see `/dev/cu.usbserial-*` instead, you've reached the second ESP32 through its CH340: flip the plug at the knob end. `tools/which-chip --watch` says which chip you've reached, twice a second, while you turn the plug.

Opening the serial port restarts the board.

## Facts checked against the schematic

The schematic is five PNG sheets, in [this zip](https://files.waveshare.com/wiki/ESP32-S3-Knob-Touch-LCD-1.8/ESP32-S3-Knob-Touch-LCD-1.8-schematic.zip).

- **Battery:** `BATT_ADC` is GPIO1 (ADC1 channel 0), halfway along two 10K resistors from the board's `5V` net to ground. That's a measure of the 5V rail, so the firmware reads 2 × the pin's voltage. There's no gauge, and the S3 has no charging signal, so `charging` is always false. Waveshare's own demo reads 4.78 to 4.88 V on USB, and those readings jump by 100 mV.
- **Speaker:** none. A PCM5100A DAC drives a 3.5 mm jack, and Waveshare says "the product does not have a built-in speaker", so `speaker` is false. The DAC's I2S lines go through a CH445P switch shared with the second ESP32. Supporting headphones would mean driving that switch.
- **Motion sensor:** none on the schematic, and Waveshare's product table has a dash for it.
- **Microphone:** one MSM261D4030H1CPM with L/R grounded, on GPIO45 (clock) and GPIO46 (data).

## Quirks

- **Detent tick:** detents play effect 26, a soft tick, at most one per 40 ms. On battery, a full-strength tick per detent dipped the supply and made the backlight flicker. Don't make it stronger without checking on battery.
- **Time's-up vibration:** real-time playback at full scale for 30 s. Don't raise the DRV2605's voltage clamp: 30 s continuous could overheat the motor.
- **The touch chip sleeps** between touches and NACKs polls. That's expected, so the driver turns off the I2C bus's error logging.
- **The knob's two lines** each pulse for one direction. They aren't a quadrature pair.
- **The DRV2605's outputs are labeled LRA** on the schematic, but this board's setup, from Waveshare's demo, drives it as an ERM motor in open loop. It has worked well on the owner's knob.

## To check on the hardware

None of this firmware has run on a board yet. Beyond the basics (does it start, does the screen light, does the touch land under the finger):

- **Battery level:** the charging board with the USB-C socket and the battery connector isn't in the published schematic, so what the 5V net does on battery is unknown. If the charger passes the cell's voltage through, as the parts on the main board suggest (a buck converter that runs from 2.5 V and no boost converter), the level works. If it boosts the cell to 5 V, this always reads as USB with an unknown level, which is harmless but useless: set `.battery = false` then. Check on battery at a few charge levels: the driver logs the rail's voltage about every 30 s. The percent curve in `drivers/adc_battery/` is a typical lithium cell's, not measured on this one.
- **USB threshold:** 4.4 V separates USB from the cell. It sits between Waveshare's 4.8 V on USB and a full cell's 4.2 V, and a drop across a diode on the charging board could move either.
- **Brightness levels:** the old firmware only ever ran at full. The ten levels are a guess at an even-looking curve: the lowest may flicker or go dark, depending on the backlight's driver at a 1/255 duty.
- **Input polling:** touch is now polled every 10 ms in its own task, faster than the old firmware's once a frame.
