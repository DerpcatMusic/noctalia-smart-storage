local busy = false
local function quote(s) return "'" .. string.gsub(s, "'", "'\\''") .. "'" end
function update()
  noctalia.setUpdateInterval(60000)
  if busy then return end
  busy = true
  noctalia.runAsync("python3 " .. quote(noctalia.pluginDir() .. "/cleaner.py") .. " status", function(r)
    busy = false
    local d = noctalia.json.decode(r.stdout or "")
    barWidget.setGlyph("database")
    if r.exitCode ~= 0 or not d or d.error then
      barWidget.setText("…")
      barWidget.setTooltip("Smart Storage — scan or cleanup in progress")
      return
    end
    local disk = d.disks and d.disks[1]
    if disk then
      barWidget.setText(string.format("%.0fG", disk.free / 1073741824))
      barWidget.setColor(disk.free / disk.total < 0.1 and "error" or "on_surface")
      barWidget.setTooltip(string.format("Smart Storage · %.1f GiB free\n%.1f GiB eligible · click to review", disk.free / 1073741824, d.eligible / 1073741824))
    end
  end)
end
function onClick() noctalia.togglePanel("derpcat/smart-storage:dashboard") end
function onRightClick() onClick() end
function onIpc(event) if event == "refresh" then update() else onClick() end end
update()
