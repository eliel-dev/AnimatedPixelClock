/*
 * hello_matrix.cpp - HUB75 RGB matrix hardware bring-up (Phase 1)
 * Project: AnimatedPixelClock
 *
 * Target: any supported ESP32-S3 board driving one 128x64 HUB75E panel
 *         (1/32 scan) = 128x64 RGB.
 *
 * Build / flash:  platformio run -e matrix-s3-bringup --target upload
 *   (or matrix-wroom-bringup / matrix-waveshare-bringup)
 *   (hold BOOT/GPIO0 while plugging USB to enter download mode on native-USB boards)
 *
 * Purpose: prove the hardware path BEFORE porting any clock animation -
 * pin map, color order, 1/32 scan, panel chaining/seam, and FM6126A init.
 *
 * --- IF THE SCREEN IS DEAD-BLACK ---
 *   Also try toggling USE_FM6126A.
 */

#include <Arduino.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

// Pin map (locked; see docs/HUB75_WIRING.md). Shared with the firmware so a
// bring-up run tests the same wiring the clock will use.
#include "display/hub75_pins.h"

// ---- Panel geometry ----
#define PANEL_W 128   // physical panel width
#define PANEL_H 64    // single module height
#define PANELS  1     // one physical panel

// Clock phase. Default-true drops the RIGHTMOST column on some panels (esp.
// FM6126A Waveshare) - the missing right-edge column / corner. Set false to fix.
// If instead the FIRST column doubles or the image shifts, set this back to true.
#define CLK_PHASE false

static MatrixPanel_I2S_DMA *dma = nullptr;
static const uint16_t TOTAL_W = PANEL_W * PANELS;   // 128 with PANELS=2

static void hold(const char *what, uint32_t ms) {
  Serial.printf("[bringup] %s\n", what);
  delay(ms);
}

void setup() {
  Serial.begin(115200);
  // Native USB-CDC enumerates AFTER boot; without this wait the first prints
  // are lost before the host attaches.
  while (!Serial && millis() < 2000) {
    delay(10);
  }
  Serial.println();
  Serial.printf("[bringup] HUB75 %dx%d (chain=%d) FM6126A=%d\n",
                TOTAL_W, PANEL_H, PANELS, USE_FM6126A);

  // i2s_pins field order is FIXED: r1,g1,b1,r2,g2,b2,a,b,c,d,e,lat,oe,clk
  HUB75_I2S_CFG::i2s_pins pins = {
      HUB75_PIN_R1, HUB75_PIN_G1, HUB75_PIN_B1,
      HUB75_PIN_R2, HUB75_PIN_G2, HUB75_PIN_B2,
      HUB75_PIN_A, HUB75_PIN_B, HUB75_PIN_C, HUB75_PIN_D, HUB75_PIN_E,
      HUB75_PIN_LAT, HUB75_PIN_OE, HUB75_PIN_CLK};

  HUB75_I2S_CFG mxconfig(PANEL_W, PANEL_H, PANELS, pins);
  mxconfig.driver = HUB75_I2S_CFG::SHIFTREG;
  mxconfig.i2sspeed = HUB75_I2S_CFG::HZ_10M;
  mxconfig.clkphase = CLK_PHASE;   // fixes dropped rightmost column (see define)
  mxconfig.latch_blanking = 2;
  mxconfig.min_refresh_rate = 60;
  mxconfig.setPixelColorDepthBits(6);
  // Internal SRAM DMA only - do NOT route the buffer through the S3-Zero's
  // slow Quad-SPI PSRAM (library default is internal SRAM, so leave as-is).

  dma = new MatrixPanel_I2S_DMA(mxconfig);
  if (!dma->begin()) {
    Serial.println("[bringup] dma->begin() FAILED (DMA alloc?) - halting");
    while (true) {
      delay(1000);
    }
  }
  dma->setBrightness8(90);   // ~35% - cap current on first power-on
  dma->clearScreen();
  Serial.println("[bringup] begin() OK - cycling test patterns");
}

void loop() {
  const uint16_t RED   = dma->color565(255, 0, 0);
  const uint16_t GREEN = dma->color565(0, 255, 0);
  const uint16_t BLUE  = dma->color565(0, 0, 255);
  const uint16_t WHITE = dma->color565(255, 255, 255);

  // 1. Solid R / G / B - verifies color order + every pixel lights
  dma->fillScreen(RED);    hold("fill RED", 2000);
  dma->fillScreen(GREEN);  hold("fill GREEN", 2000);
  dma->fillScreen(BLUE);   hold("fill BLUE", 2000);

  // 2. Full white - brief current sanity check (max draw on both panels)
  dma->fillScreen(WHITE);  hold("fill WHITE (brief)", 800);

  // 3. 1px border - edges + correct 1/32 scan. drawRect args = (x, y, w, h)
  dma->clearScreen();
  dma->drawRect(0, 0, TOTAL_W, PANEL_H, WHITE);
  hold("border rect", 2000);

  // 4. Corner pixels - origin + canvas extents
  dma->clearScreen();
  dma->drawPixel(0, 0, WHITE);
  dma->drawPixel(TOTAL_W - 1, 0, WHITE);
  dma->drawPixel(0, PANEL_H - 1, WHITE);
  dma->drawPixel(TOTAL_W - 1, PANEL_H - 1, WHITE);
  hold("4 corners", 2000);

  // 5. Diagonal - both panels read as one continuous 128-wide canvas
  dma->clearScreen();
  dma->drawLine(0, 0, TOTAL_W - 1, PANEL_H - 1, WHITE);
  hold("diagonal", 2000);

  // 6. Half-panel test - left half RED, right half BLUE.
  dma->fillRect(0, 0, TOTAL_W / 2, PANEL_H, RED);
  dma->fillRect(TOTAL_W / 2, 0, TOTAL_W - TOTAL_W / 2, PANEL_H, BLUE);
  hold("seam test (L=red R=blue)", 3000);
}
