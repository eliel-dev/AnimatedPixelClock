#pragma once
// ============================================================
// lua_effects.h - the Lua effects, as pages the panel can show
// ============================================================
// Uploaded scripts are listed in the portal and shown as a full-screen page.
// These calls are safe from loop() - which is where WebServer handlers run.
// Only envs built with LUA_EFFECTS_ENABLED define them, so
// guard any call with #if defined(LUA_EFFECTS_ENABLED).
// ============================================================

#include <stddef.h>
#include <stdint.h>

#if defined(LUA_EFFECTS_ENABLED) && !defined(NSLUA_ENABLED)
#error "LUA_EFFECTS_ENABLED needs NSLUA_ENABLED: the effects run on the nslua runtime"
#endif
uint8_t     luaEffectCount();             // compiled-in plus uploaded; changes at run time
uint8_t     luaEffectSlots();             // how many pages are reserved, empty ones included
bool        luaEffectUploaded(uint8_t i); // true when this one came over the air
// The name, copied into the caller's buffer. Not a pointer: an uploaded
// script's name lives in a table that an upload or a delete rewrites.
void        luaEffectName(uint8_t i, char *out, size_t cap);
#define LUA_EFFECT_NAME_CAP 25
void        luaEffectShow(uint8_t i);     // make it the page on screen now
int16_t     luaEffectCurrent();           // -1 when none is showing
// Step off whatever is running. Deleting an uploaded script moves every index
// above it, so the selection has to be dropped rather than left pointing at a
// slot that now holds something else.
void        luaEffectStop();
// Reopen what is on screen without changing which one it is: after an upload
// replaces the running script, or after a delete renumbers the slots.
void        luaEffectsReload();

// The effect task's stack high-water mark, in bytes still free at its worst.
// Exposed because this firmware's whole case for running uploaded scripts is an
// arithmetic about that number, and an arithmetic nobody can read off the
// running panel is a claim rather than a fact. 0 when the task is not up.
uint32_t    luaEffectsStackFreeMin();
// A click for the effect on screen (px.button): the knob's click and the
// remote's OK on an effect page, and POST /api/lua {"click":true}. loop() only.
void        luaEffectsClick();
// The effect on screen for the twin's sync: its index (-1 none), how many times
// an effect has been chosen since boot, the clicks since it was chosen, and
// whether its first frame has arrived.
void        luaEffectsFx(int16_t *id, uint32_t *run, uint32_t *clicks, bool *open);
const char *luaEffectsTrialReport();   // the last upload's trial run, "" before one
