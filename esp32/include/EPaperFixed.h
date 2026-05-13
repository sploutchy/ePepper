/**
 * Local subclass of Seeed_GFX's EPaper with a corrected partial-refresh path.
 *
 * The stock updataPartial() in Seeed_GFX only pushes NEW image data (cmd 0x13)
 * and never touches OLD image data (cmd 0x10). The UC8179's partial-refresh
 * differential LUT compares OLD vs NEW per pixel; if OLD is stale, pixels that
 * should transition black→white don't get told to flip, so each partial refresh
 * overlays on top of the previous one (visible as scrambled clock text).
 *
 * This override pushes the same content as OLD *after* the refresh completes,
 * so the controller's OLD register matches what's now displayed on the panel.
 * On the next partial refresh, the differential LUT then starts from the
 * correct baseline.
 */

#ifndef EPEPPER_EPAPER_FIXED_H
#define EPEPPER_EPAPER_FIXED_H

#include "TFT_eSPI.h"

class EPaperFixed : public EPaper {
public:
    void updataPartial(uint16_t x, uint16_t y, uint16_t w, uint16_t h);
};

#endif
