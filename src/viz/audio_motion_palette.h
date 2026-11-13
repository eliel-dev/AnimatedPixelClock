#pragma once

#include <stdint.h>

namespace audio_motion_palette {

struct RgbColor888 {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

struct PaletteStop {
  float offset;
  RgbColor888 color;
};

static inline float clamp01(float value) {
  if (value <= 0.0f) return 0.0f;
  return value >= 1.0f ? 1.0f : value;
}

static inline uint8_t lerpChannel(uint8_t start, uint8_t end, float amount) {
  const float value = static_cast<float>(start) +
                      (static_cast<float>(end) - start) * amount;
  if (value <= 0.0f) return 0;
  if (value >= 255.0f) return 255;
  return static_cast<uint8_t>(value);
}

static inline RgbColor888 sample(float t, uint8_t style) {
  // Stops and offsets mirror the live Mica Audio Win2D presets.
  static const PaletteStop rainbow[] = {
      {0.00f, {255, 30, 30}},  {0.16f, {255, 116, 0}},
      {0.33f, {255, 236, 0}},  {0.50f, {0, 255, 80}},
      {0.67f, {0, 220, 255}},  {0.83f, {0, 80, 255}},
      {1.00f, {255, 0, 255}},
  };
  static const PaletteStop sunset[] = {
      {0.00f, {255, 72, 40}},   {0.20f, {255, 128, 32}},
      {0.40f, {255, 186, 52}},  {0.60f, {255, 108, 84}},
      {0.80f, {196, 66, 255}},  {1.00f, {88, 54, 255}},
  };
  static const PaletteStop arctic[] = {
      {0.00f, {0, 214, 255}},    {0.20f, {0, 168, 255}},
      {0.40f, {74, 224, 255}},   {0.60f, {144, 255, 219}},
      {0.80f, {68, 128, 255}},   {1.00f, {144, 72, 255}},
  };
  static const PaletteStop neon[] = {
      {0.00f, {57, 255, 20}},    {0.18f, {0, 255, 180}},
      {0.36f, {0, 220, 255}},    {0.54f, {45, 120, 255}},
      {0.72f, {196, 80, 255}},   {0.88f, {255, 46, 178}},
      {1.00f, {255, 120, 64}},
  };

  const PaletteStop* stops = rainbow;
  uint8_t count = sizeof(rainbow) / sizeof(rainbow[0]);
  if (style == 9) {
    stops = sunset;
    count = sizeof(sunset) / sizeof(sunset[0]);
  } else if (style == 10) {
    stops = arctic;
    count = sizeof(arctic) / sizeof(arctic[0]);
  } else if (style == 11) {
    stops = neon;
    count = sizeof(neon) / sizeof(neon[0]);
  }

  t = clamp01(t);
  for (uint8_t i = 1; i < count; ++i) {
    if (t <= stops[i].offset) {
      const PaletteStop& left = stops[i - 1];
      const PaletteStop& right = stops[i];
      const float range = right.offset - left.offset;
      float localT = (t - left.offset) / range;
      localT = clamp01(localT);
      return {
          lerpChannel(left.color.r, right.color.r, localT),
          lerpChannel(left.color.g, right.color.g, localT),
          lerpChannel(left.color.b, right.color.b, localT),
      };
    }
  }

  return stops[count - 1].color;
}

}  // namespace audio_motion_palette
