# Bundled UHD images for the B220 Mini (LibreSDR clone)

The **SUOGOEST B220 Mini** is a LibreSDR-family USRP B210 clone built on a Xilinx
**Artix-7 XC7A100T**, not the genuine B210's Spartan-6. The stock Ettus
`usrp_b210_fpga.bin` is a Spartan-6 bitstream and can NEVER configure this board —
it fails deterministically with `fx3 is in state 5` (FPGA DONE never asserts).

These are the images the app loads for the B220 (`B220Source._ensure_uhd_images`
installs `usrp_b210_fpga.bin` into UHD's images dir at connect, backing up any
Ettus original as `*.ettus-orig`):

| file | bytes | sha256 | source |
|---|---|---|---|
| `usrp_b210_fpga.bin` | 2,900,396 | `0ab54295…d03bdca` | LibreSDR XC7A100T bitstream — github.com/lmesserStep/LibreSDRB210 (verified working on this unit 2026-07-06) |
| `usrp_b200_fw.hex` | 514,346 | (Ettus original) | FX3 firmware, unchanged from UHD 4.10 — clones share Ettus firmware |

If a future unit is the **XC7A200T** variant instead (the 200T bitstream is
~4.34 MB, sha256 `7322a2a9…46ce5`, from github.com/NustyFrozen/LibreSDR-UHD-B220-Mini
or alphafox02/LibreSDR_USRP — byte-identical), swap it in here. A wrong-size
bitstream fails harmlessly with the same state-5 error, so trying both is safe.

FPGA images are RAM-loaded over USB every session — nothing is flashed, and a
power cycle reverts to a blank FPGA. Fully reversible.
