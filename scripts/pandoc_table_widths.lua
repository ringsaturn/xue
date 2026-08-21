-- Recompute pandoc table column widths from the widest cell in each column.
--
-- Pipe tables get their relative widths from the dashes in the source
-- separator row, which in docs/format.md are all the same length, so every
-- column comes out equally wide and the prose columns wrap to shreds while
-- the numeric ones sit half empty. Measuring the rendered text instead gives
-- each column room in proportion to what it actually holds.

local stringify = pandoc.utils.stringify

-- A wide cell is wrapped by LaTeX, so its influence on the column width is
-- damped rather than linear; without this a single long sentence starves
-- every other column.
local function weight(len)
  return math.sqrt(math.max(len, 1))
end

local function measure(row, widths)
  local col = 1
  for _, cell in ipairs(row.cells) do
    local len = #stringify(cell.contents)
    for _ = 1, cell.col_span do
      widths[col] = math.max(widths[col] or 0, len)
      col = col + 1
    end
  end
end

function Table(tbl)
  local ncols = #tbl.colspecs
  if ncols == 0 then return nil end

  local widths = {}
  for _, row in ipairs(tbl.head.rows) do measure(row, widths) end
  for _, body in ipairs(tbl.bodies) do
    for _, row in ipairs(body.body) do measure(row, widths) end
  end

  local total = 0
  for col = 1, ncols do
    widths[col] = weight(widths[col] or 1)
    total = total + widths[col]
  end
  if total == 0 then return nil end

  -- 0.97 leaves the inter-column padding somewhere to go.
  for col = 1, ncols do
    tbl.colspecs[col][2] = 0.97 * widths[col] / total
  end
  return tbl
end
