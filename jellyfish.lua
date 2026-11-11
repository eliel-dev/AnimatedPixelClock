-- Single-effect prototype for the 128x64 HUB75 display.
-- Upload this file from the local-effects page in the clock portal.

FPS = 20
PERIOD = 12

local pi = math.pi
local sin = math.sin
local floor = math.floor

function draw()
  local t = px.t() * 2 * pi
  local cx = floor(64 + sin(t) * 9)
  local cy = floor(22 + sin(t * 2) * 3)

  px.clear(1, 5, 18)

  -- A few dim bubbles give the dark water some depth.
  for i = 0, 4 do
    local x = (i * 29 + math.floor(t * 7)) % 128
    local y = (i * 13 + 8 - math.floor(t * 5)) % 64
    px.circle(x, y, 1, 20, 66, 104, false)
  end

  -- Trailing tentacles, each split into short moving segments.
  for i = -2, 2 do
    local x0 = cx + i * 3
    local y0 = cy + 8
    for j = 1, 3 do
      local y1 = cy + 8 + j * 7
      local sway = sin(t * 1.4 + i * 0.8 + j * 0.55) * (j * 1.5)
      local sway0 = sin(t * 1.4 + i * 0.8 + (j - 1) * 0.55) * ((j - 1) * 1.5)
      px.line(floor(x0 + sway0), y0, floor(x0 + sway), y1, 42, 194, 255)
      x0, y0 = x0 + sway, y1
    end
  end

  -- Bell: round crown with a flat lower edge, plus a bright rim.
  px.circle(cx, cy, 9, 30, 154, 226, true)
  px.rect(cx - 8, cy, 17, 5, 30, 154, 226, true)
  px.circle(cx, cy, 9, 100, 236, 255, false)
  px.line(cx - 7, cy + 4, cx + 7, cy + 4, 165, 250, 255)

  -- Small oral arms beneath the bell.
  px.line(cx - 3, cy + 5, cx - 4, cy + 10, 90, 224, 255)
  px.line(cx, cy + 5, cx, cy + 11, 90, 224, 255)
  px.line(cx + 3, cy + 5, cx + 4, cy + 10, 90, 224, 255)

  -- Soft light around the creature, composited into the same RGB canvas.
  px.glow(cx, cy + 5, 12, 18, 96, 255, 0.22)
end
