# amoled-2.16

[Waveshare ESP32-S3-Touch-AMOLED-2.16](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-2.16): a 480 × 480 AMOLED (CO5300) with a CST9217 touch controller, an ES8311 and speaker, two microphones on an ES7210, a QMI8658 motion sensor and an AXP2101 power chip, on an ESP32-S3R8 with 16 MB flash.

Its [schematic](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-2.16/ESP32-S3-Touch-AMOLED-2.16-Schematic.pdf) has the board's layout on its second and third pages.

## USB

One USB-C socket, wired directly to the ESP32-S3: it shows up as `/dev/cu.usbmodem*`. Opening the serial port restarts the board.

## Facts checked against the schematic

- **Microphones:** MIC1 and MIC2 are analog MEMS mics, AC-coupled into the ES7210's MIC1 and MIC2 inputs and powered from its `MICBIAS12`. Its `SDOUT1` reaches GPIO10 (`I2S_ASDOUT`) through a 51 Ω resistor. It shares MCLK (GPIO42), BCLK (GPIO9) and LRCK (GPIO45) with the ES8311, and its A0 and A1 pins are grounded, which is the schematic's "0x40". Waveshare's BSP opens the same chip for the mic. The ES8311 isn't involved: the BSP sets it to DAC only, and its `ASDOUT` doesn't reach the S3. The ES7210's MIC3 input hears the speaker's output through a filter, labeled AEC, for echo cancelling, and nothing uses that yet.
- **Motion sensor axes:** the silk beside the QMI8658 on the layout shows them. The chip is on the side facing the panel. Seen from the front, with the buttons at the top, X points right, Y points up and Z points out of the screen.

## Quirks

- **Panel windows:** the CO5300 needs a window to start on an even row and end on an odd one. `HAL_BAND_ROWS` is 24, so every band does.
- **Buttons:** PWR reads high when pressed (through a transistor), and holding it powers the board off through the AXP2101. BOOT and User read low. Holding BOOT during a reset starts the chip in download mode.
- **Audio clocks:** in full duplex, the I2S transmit side makes the clocks, so playback stays enabled all the time, and the mic's receive side comes on only while it records.
- **Old keys:** Bento's store moved in flash, so a board coming from the zoo starts with no pairing keys. A Mac that paired with the zoo must forget it in Bluetooth settings before pairing again.

## To check on the hardware

None of this firmware has run on a board yet, and these parts have never run in any earlier firmware:

- **The mic:** the ES7210 at 16 kHz stereo, with both mics averaged to mono. Check that the hardware test's `m` command reads levels that follow your voice, and whether 30 dB of gain suits it.
- **Speaker and mic together:** they share one I2S port. Check that sounds still play while the mic records.
- **Which way the motion axes go:** the signs in `board.c` come from the silk and the zoo's unverified note that face down reads about -1 g. Check with the hardware test's log: flat on a desk, screen up, it should read z about -1, and upright on its USB edge, y about +1.

Still unverified from the zoo: the speaker's volume steps and the battery percent.
