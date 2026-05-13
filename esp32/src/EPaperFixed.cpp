#include "EPaperFixed.h"

void EPaperFixed::updataPartial(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
    int32_t bx = x;
    int32_t by = y;
    int32_t bw = w;
    int32_t bh = h;
    switch (rotation & 3) {
        case 1:
            bx = (int32_t)_width - y - h;
            by = x;
            bw = h;
            bh = w;
            break;
        case 2:
            bx = (int32_t)_width - x - w;
            by = (int32_t)_height - y - h;
            break;
        case 3:
            bx = y;
            by = (int32_t)_height - x - w;
            bw = h;
            bh = w;
            break;
        default:
            break;
    }

    if (bx < 0) { bw += bx; bx = 0; }
    if (by < 0) { bh += by; by = 0; }
    if ((bx + bw) > (int32_t)_width)  bw = (int32_t)_width - bx;
    if ((by + bh) > (int32_t)_height) bh = (int32_t)_height - by;
    if (bw < 1 || bh < 1) return;

    uint16_t align_px = 8;
#ifdef TCON_ENABLE
    align_px = 16;
    wake();
#else
    EPD_WAKEUP_PARTIAL();
#endif

    uint16_t x0 = ((uint16_t)bx) & ~(align_px - 1);
    uint16_t x1 = ((uint16_t)(bx + bw + (align_px - 1))) & ~(align_px - 1);
    uint16_t w_aligned = x1 - x0;
    uint16_t yy = (uint16_t)by;
    uint16_t hh = (uint16_t)bh;

    uint16_t stride = _width >> 3;
    uint16_t win_bytes_per_row = w_aligned >> 3;
    const uint8_t* src0 = _img8 + (yy * stride) + (x0 >> 3);

    size_t win_size = (size_t)win_bytes_per_row * hh;
    uint8_t* winbuf = (uint8_t*)malloc(win_size);
    if (!winbuf) return;

    for (uint16_t row = 0; row < hh; row++) {
        memcpy(winbuf + row * win_bytes_per_row,
               src0  + row * stride,
               win_bytes_per_row);
    }

#ifdef EPD_HORIZONTAL_MIRROR
    uint16_t x_end = x0 + w_aligned - 1;
    uint16_t mx0 = (_width - 1) - x_end;
    uint16_t mx1 = (_width - 1) - x0;
    EPD_SET_WINDOW(mx0, yy, mx1, yy + hh - 1);
    EPD_PUSH_NEW_COLORS_FLIP(w_aligned, hh, winbuf);
#else
    EPD_SET_WINDOW(x0, yy, x0 + w_aligned - 1, yy + hh - 1);
    EPD_PUSH_NEW_COLORS(w_aligned, hh, winbuf);
#endif
    // CDI override: EPD_SET_WINDOW just set the border-mode bits to 0xA9
    // (VBD=10 = LUTWB, drives border white→black on refresh — the source of
    // the visible gray frame). Re-write CDI with VBD=01 (LUTBW) so the border
    // is actively driven to white instead.
    writecommand(0x50);
    writedata(0x69);
    writedata(0x07);
    EPD_UPDATE_PARTIAL();

    // Re-push the same content as OLD so the controller's OLD register now
    // matches what's actually displayed. Without this, the next call here
    // sees stale OLD and the differential LUT leaves previous-frame pixels in
    // place (scrambled clock text). EPD_UPDATE_PARTIAL above is blocking so
    // it's safe to talk to the controller again here.
#ifdef EPD_HORIZONTAL_MIRROR
    EPD_SET_WINDOW(mx0, yy, mx1, yy + hh - 1);
    EPD_PUSH_OLD_COLORS_FLIP(w_aligned, hh, winbuf);
#else
    EPD_SET_WINDOW(x0, yy, x0 + w_aligned - 1, yy + hh - 1);
    EPD_PUSH_OLD_COLORS(w_aligned, hh, winbuf);
#endif

    free(winbuf);
    EPaper::sleep();
}
