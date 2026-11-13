#include <assert.h>

#include "../src/viz/audio_motion_palette.h"

static void expectColor(uint8_t style, float t, uint8_t r, uint8_t g, uint8_t b) {
  const audio_motion_palette::RgbColor888 actual =
      audio_motion_palette::sample(t, style);
  assert(actual.r == r);
  assert(actual.g == g);
  assert(actual.b == b);
}

int main() {
  expectColor(8, 0.00f, 255, 30, 30);
  expectColor(8, 0.16f, 255, 116, 0);
  expectColor(8, 0.33f, 255, 236, 0);
  expectColor(8, 0.50f, 0, 255, 80);
  expectColor(8, 0.67f, 0, 220, 255);
  expectColor(8, 0.83f, 0, 80, 255);
  expectColor(8, 1.00f, 255, 0, 255);

  expectColor(9, 0.00f, 255, 72, 40);
  expectColor(9, 0.20f, 255, 128, 32);
  expectColor(9, 0.40f, 255, 186, 52);
  expectColor(9, 0.60f, 255, 108, 84);
  expectColor(9, 0.80f, 196, 66, 255);
  expectColor(9, 1.00f, 88, 54, 255);
  expectColor(9, 0.70f, 225, 87, 169);

  expectColor(10, 0.00f, 0, 214, 255);
  expectColor(10, 0.20f, 0, 168, 255);
  expectColor(10, 0.40f, 74, 224, 255);
  expectColor(10, 0.60f, 144, 255, 219);
  expectColor(10, 0.80f, 68, 128, 255);
  expectColor(10, 1.00f, 144, 72, 255);

  expectColor(11, 0.00f, 57, 255, 20);
  expectColor(11, 0.18f, 0, 255, 180);
  expectColor(11, 0.36f, 0, 220, 255);
  expectColor(11, 0.54f, 45, 120, 255);
  expectColor(11, 0.72f, 196, 80, 255);
  expectColor(11, 0.88f, 255, 46, 178);
  expectColor(11, 1.00f, 255, 120, 64);
  expectColor(11, 0.90f, 255, 58, 159);

  expectColor(8, -0.5f, 255, 30, 30);
  expectColor(11, 1.5f, 255, 120, 64);
  return 0;
}
