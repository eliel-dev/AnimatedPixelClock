/*
 * AnimatedPixelClock - Custom Animation Store
 *
 * LittleFS-backed storage for user-uploaded .pca animations (legacy PCA1 and
 * full-color PCA2 produced by the PC companion) played by ambient
 * style. The 128KiB partition on 4MB boards supports short clips; upload
 * capacity is calculated from current free space with a filesystem margin.
 */

#ifndef ANIM_STORE_H
#define ANIM_STORE_H

#include <Arduino.h>
#include <FS.h>

// PCA1 is legacy indexed 4bpp; PCA2 stores full RGB565 frames.
#define PCA_MAGIC "PCA1"
#define PCA2_MAGIC "PCA2"
#define PCA_HEADER_BYTES 12
#define PCA_MAX_FRAMES 360
#define PCA_MAX_PALETTE 16
#define PCA_FRAME_BYTES 4096  // 128*64 / 2
#define PCA2_FRAME_BYTES 16384 // 128*64 RGB565
#define PCA_MAX_BYTES (1536UL * 1024UL)  // hard upload cap

#define ANIM_DIR "/anim"
#define ANIM_TMP "/anim/upload.tmp"
#define ANIM_FS_MIN_TOTAL (64UL * 1024UL)  // small partitions support short clips
#define ANIM_FS_FREE_MARGIN (16UL * 1024UL)

struct PcaHeader {
  uint16_t frameCount;
  uint16_t defaultFrameMs;
  uint8_t paletteLen;
  uint8_t flags;
  uint8_t format; // 1=PCA1 indexed, 2=PCA2 RGB565
  uint32_t frameBytes;
};

// Mount LittleFS (formats on first use) and create the anim directory.
void animStoreInit();

// True when the filesystem mounted and is large enough for the feature.
bool animFsUsable();

size_t animFsFree();
size_t animFsTotal();
// Refresh after storage mutations only; status/render paths use cached values.
void animFsRefresh();

// Strict basename check: [A-Za-z0-9_-]{1,24}, no path parts, no extension.
bool animValidName(const char* name);

// Parse + bounds-check the PCA header and verify the EXACT expected file
// length. Leaves the file position unspecified. hdr may be nullptr.
bool animValidatePca(File& f, PcaHeader* hdr);

// Full path "/anim/<name>.pca" for a validated basename.
String animPath(const char* name);

#endif  // ANIM_STORE_H
