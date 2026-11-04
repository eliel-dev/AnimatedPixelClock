/*
 * AnimatedPixelClock - Ambient: Custom Animation
 *
 * Plays a user-uploaded .pca animation (see anim_store.h) selected by
 * settings.ambientCustomFile. Header, palette and the per-frame delay table
 * are cached at open. Rendering matches the This-is-fine player: equal-color
 * runs become single HLine calls. Missing or corrupt files fall back to the
 * Space Invaders effect.
 *
 * Two hardware-earned rules shape this file:
 *
 * 1. No File handle is ever kept across calls. littlefs does not support the
 *    same file being open twice, and /api/anim/list's directory iteration
 *    opens every file - a held playback handle got its reads corrupted by
 *    that (short read mid-play). Every read is open/seek/read/close, and
 *    everything runs sequentially on the loop task, so two handles to one
 *    file cannot coexist.
 *
 * 2. No filesystem I/O inside the render tick. A flash read between
 *    clearDisplay() and the buffer flip delays the flip by a few ms on
 *    every frame advance; that phase jitter against the panel's DMA scan
 *    shows as mirrored rolling bands (same physics as the old 60 Hz beat).
 *    The next frame is therefore prefetched into a second buffer from
 *    loop() BETWEEN render ticks (ambientCustomPrefetch), and the advance
 *    just swaps buffer pointers.
 */

#include <LittleFS.h>

#include "../config/config.h"
#include "../display/display.h"
#include "ambient.h"
#include "anim_store.h"

static PcaHeader pcaHdr;
static uint16_t pcaPalette[PCA_MAX_PALETTE];
// Fixed buffers, never heap-allocated: an open attempt right after a save
// runs while the config page reload has the heap under pressure (lwIP
// buffers + page streaming), and a failed 4KB malloc there stranded the
// player on the Invaders fallback until the user saved again (hardware-seen,
// failReason=alloc). ~9KB of BSS buys an open path that cannot OOM.
static uint16_t pcaDelays[PCA_MAX_FRAMES];
static uint8_t pcaBufA[PCA2_FRAME_BYTES];
static uint8_t pcaBufB[PCA2_FRAME_BYTES];
static uint8_t* pcaFrame = pcaBufA;    // frame being drawn
static uint8_t* pcaNext = pcaBufB;     // prefetched next frame
static int pcaNextIndex = -1;          // frame held in pcaNext (-1 = none)
static bool pcaWantPrefetch = false;   // loop() should fetch pcaNextIndex
static int pcaRequestedIndex = -1;     // frame requested by the timeline
static uint32_t pcaFrameOffset = 0;    // file offset of frame 0
static int pcaIndex = -1;
static uint32_t pcaPlaybackStartedMs = 0;
static uint32_t pcaLastRenderMs = 0;
static uint32_t pcaTotalDurationMs = 0;
static bool pcaOpen = false;
static bool pcaFailed = false;         // last attempt failed; retried on a timer
static unsigned long pcaRetryAt = 0;   // next automatic retry (2s backoff)
// Why the last open/playback attempt failed (kept across invalidate for
// /api/anim/list diagnostics): 0=ok 1=gate 2=validate 3=header-read
// 4=first-frame 5=prefetch-read 6=alloc
static uint8_t pcaFailReason = 0;

bool ambientCustomPlaying() { return pcaOpen && !pcaFailed; }

uint8_t ambientCustomFailReason() { return pcaFailReason; }

void ambientCustomInvalidate() {
  pcaNextIndex = -1;
  pcaRequestedIndex = -1;
  pcaWantPrefetch = false;
  pcaOpen = false;
  pcaFailed = false;
  pcaIndex = -1;
  pcaTotalDurationMs = 0;
}

// Transient open per read - rule 1 in the header comment.
static bool pcaReadFrame(int index, uint8_t* dst) {
  File f = LittleFS.open(animPath(settings.ambientCustomFile), "r");
  if (!f) return false;
  bool ok = f.seek(pcaFrameOffset + (uint32_t)index * pcaHdr.frameBytes) &&
            f.read(dst, pcaHdr.frameBytes) == pcaHdr.frameBytes;
  f.close();
  return ok;
}

static bool pcaTryOpen() {
  if (!animFsUsable() || !animValidName(settings.ambientCustomFile)) {
    pcaFailReason = 1;
    return false;
  }

  File f = LittleFS.open(animPath(settings.ambientCustomFile), "r");
  if (!animValidatePca(f, &pcaHdr)) {
    pcaFailReason = 2;
    if (f) f.close();
    return false;
  }

  uint8_t palRaw[PCA_MAX_PALETTE * 2];
  bool ok = f.seek(PCA_HEADER_BYTES) &&
            (pcaHdr.paletteLen == 0 || f.read(palRaw, pcaHdr.paletteLen * 2) == (size_t)pcaHdr.paletteLen * 2) &&
            f.read((uint8_t*)pcaDelays, pcaHdr.frameCount * 2) ==
                (size_t)pcaHdr.frameCount * 2;
  f.close();
  if (!ok) {
    pcaFailReason = 3;
    return false;
  }
  for (int i = 0; i < pcaHdr.paletteLen; i++)
    pcaPalette[i] = (uint16_t)palRaw[i * 2] | ((uint16_t)palRaw[i * 2 + 1] << 8);
  // Preserve the source GIF timeline. The display itself is sampled at 30 Hz,
  // so short frames may be skipped by the timeline selector below.
  pcaTotalDurationMs = 0;
  for (int i = 0; i < pcaHdr.frameCount; i++) {
    if (pcaDelays[i] < 1) pcaDelays[i] = 1;
    if (pcaDelays[i] > 5000) pcaDelays[i] = 5000;
    pcaTotalDurationMs += pcaDelays[i];
  }
  if (pcaTotalDurationMs == 0) pcaTotalDurationMs = 100;
  pcaFrameOffset = PCA_HEADER_BYTES + (uint32_t)pcaHdr.paletteLen * 2 +
                   (uint32_t)pcaHdr.frameCount * 2;
  pcaIndex = 0;
  if (!pcaReadFrame(0, pcaFrame)) {  // one-time hitch at open is fine
    pcaFailReason = 4;
    return false;
  }
  pcaPlaybackStartedMs = millis();
  pcaLastRenderMs = pcaPlaybackStartedMs;
  pcaNextIndex = -1;
  pcaRequestedIndex = pcaHdr.frameCount > 1 ? 1 : -1;
  pcaWantPrefetch = pcaRequestedIndex >= 0;  // load the next frame in idle loop time
  pcaOpen = true;
  pcaFailReason = 0;
  return true;
}

// Called from loop() between render ticks - rule 2 in the header comment.
void ambientCustomPrefetch() {
  if (!pcaOpen || !pcaWantPrefetch) return;
  int requested = pcaRequestedIndex;
  if (requested >= 0 && pcaReadFrame(requested, pcaNext)) {
    pcaNextIndex = requested;
  } else {  // file vanished (deleted mid-play?) - fail, render falls back
    ambientCustomInvalidate();
    pcaFailed = true;
    pcaRetryAt = millis() + 2000;
    pcaFailReason = 5;
  }
  pcaWantPrefetch = false;
}

static void pcaDrawFrame() {
  if (pcaHdr.format == 2) {
    for (int y = 0; y < SCREEN_HEIGHT; ++y) {
      const uint8_t* row = pcaFrame + y * SCREEN_WIDTH * 2;
      int x = 0;
      while (x < SCREEN_WIDTH) {
        int at = x * 2;
        uint16_t color = (uint16_t)row[at] | ((uint16_t)row[at + 1] << 8);
        int run = 1;
        while (x + run < SCREEN_WIDTH) {
          int next = (x + run) * 2;
          uint16_t nextColor = (uint16_t)row[next] | ((uint16_t)row[next + 1] << 8);
          if (nextColor != color) break;
          ++run;
        }
        display.drawFastHLine(x, y, run, color);
        x += run;
      }
    }
    return;
  }
  for (int y = 0; y < SCREEN_HEIGHT; y++) {
    const uint8_t* row = pcaFrame + y * (SCREEN_WIDTH / 2);
    int x = 0;
    while (x < SCREEN_WIDTH) {
      uint8_t b = row[x >> 1];
      uint8_t idx = (x & 1) ? (b & 0x0F) : (b >> 4);
      int run = 1;
      while (x + run < SCREEN_WIDTH) {
        uint8_t nb = row[(x + run) >> 1];
        uint8_t nidx = ((x + run) & 1) ? (nb & 0x0F) : (nb >> 4);
        if (nidx != idx) break;
        run++;
      }
      display.drawFastHLine(x, y, run, pcaPalette[idx & (PCA_MAX_PALETTE - 1)]);
      x += run;
    }
  }
}

void ambientCustomFrame() {
  if (!pcaOpen) {
    // A failed attempt shows the Invaders fallback but retries every 2s: open
    // can fail transiently (e.g. during the post-save page-reload burst)
    // and must not strand the user on the fallback until the next manual save.
    if (pcaFailed && millis() < pcaRetryAt) {
      ambientInvadersFrame();
      return;
    }
    if (!pcaTryOpen()) {
      if (!pcaFailed) {
        Serial.printf("AnimStore: cannot play '%s' (reason %d), retrying\n",
                      settings.ambientCustomFile, pcaFailReason);
      }
      pcaFailed = true;
      pcaRetryAt = millis() + 2000;
      ambientInvadersFrame();
      return;
    }
    pcaFailed = false;
  }

  uint32_t now = millis();
  // If the ambient screen was inactive for a while, keep the current frame
  // and restart its duration instead of jumping ahead in the GIF.
  if (now - pcaLastRenderMs > 2000) {
    uint32_t offset = 0;
    for (int i = 0; i < pcaIndex; ++i) offset += pcaDelays[i];
    pcaPlaybackStartedMs = now - offset;
  }
  pcaLastRenderMs = now;

  uint32_t position = (now - pcaPlaybackStartedMs) % pcaTotalDurationMs;
  uint32_t elapsed = 0;
  int target = pcaHdr.frameCount - 1;
  for (int i = 0; i < pcaHdr.frameCount; ++i) {
    elapsed += pcaDelays[i];
    if (position < elapsed) {
      target = i;
      break;
    }
  }

  if (target != pcaIndex && pcaNextIndex == target) {
    // Swap in the already-prefetched frame; no filesystem I/O on this path.
    uint8_t* t = pcaFrame;
    pcaFrame = pcaNext;
    pcaNext = t;
    pcaIndex = target;
    pcaNextIndex = -1;
    pcaRequestedIndex = pcaHdr.frameCount > 1 ? (target + 1) % pcaHdr.frameCount : -1;
    pcaWantPrefetch = pcaRequestedIndex >= 0;
  } else if (target != pcaIndex && pcaRequestedIndex != target) {
    // The playback clock can pass several short source frames between display
    // ticks. Ask the idle-loop prefetcher for the latest due frame directly.
    pcaNextIndex = -1;
    pcaRequestedIndex = target;
    pcaWantPrefetch = true;
  }
  pcaDrawFrame();
}
