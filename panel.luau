local data, render, refresh
local busy, polling = false, false
local tab, message, confirmation = "clean", "", nil
local page = 1
local adminConfirm=nil
local function quote(s) return "'"..string.gsub(s,"'","'\\''").."'" end
local function size(n) return n>=1073741824 and string.format("%.1f GiB",n/1073741824) or string.format("%.0f MiB",n/1048576) end
local function short(s) return #s>54 and "…"..string.sub(s,-53) or s end
local function text(s,color,font) return ui.label({text=s,color=color or "on_surface",fontSize=font or 13,maxWidth=680,maxLines=2}) end
local function run(action, extra)
  if busy then return end
  busy=true;confirmation=nil;message="Working…";render()
  noctalia.runAsync("python3 "..quote(noctalia.pluginDir().."/cleaner.py").." start --operation "..action..(extra or ""),function(r)
    if r.exitCode~=0 then
      busy=false;local d=noctalia.json.decode(r.stdout or "");message=d and d.error or "Could not start operation";render()
    else refresh() end
  end)
end
refresh=function()
  if polling then return end
  polling=true
  noctalia.runAsync("python3 "..quote(noctalia.pluginDir().."/cleaner.py").." status",function(r)
    polling=false;local d=noctalia.json.decode(r.stdout or "")
    if r.exitCode~=0 or not d or d.error then message=d and d.error or "Status unavailable"
    else
      data=d;busy=d.job and d.job.running or false
      message=busy and ("Running "..d.job.operation.."… continues if you close this panel.") or ""
      if d.job and d.job.error and d.job.error~="" then message=d.job.error end
    end
    render()
  end)
end
local function button(label,fn,variant,enabled)
  return ui.button({text=label,onClick=fn,variant=variant or "ghost",enabled=enabled~=false and not busy,controlSize="sm"})
end
local function setTab(id) tab=id;page=1;confirmation=nil;render() end
local function toggle(id,value)
  local ids={}
  for _,g in ipairs(data.groups) do
    if (g.id==id and value=="true") or (g.id~=id and g.enabled) then table.insert(ids,g.id) end
  end
  run("configure"," --categories "..quote(table.concat(ids,",")))
end
local function metric(title,value)
  return ui.column({padding=10,gap=4,fill="surface_variant",radius=10,flexGrow=1},{text(title,"on_surface_variant",11),text(value,"primary",22)})
end
render=function()
  local body={}
  if not data then table.insert(body,text("Loading storage…"))
  elseif confirmation then
    table.insert(body,text("Permanent deletion","error",22))
    table.insert(body,text("Delete "..size(data.eligible).." of eligible data in the selected categories. This bypasses the recycle bin and cannot be undone."))
    table.insert(body,text("Active files and changed items are checked again immediately before removal."))
    table.insert(body,button("Delete permanently",function() run("delete") end,"destructive"))
    table.insert(body,button("Cancel",function() confirmation=nil;render() end))
  elseif tab=="clean" then
    table.insert(body,text("REGENERABLE DATA · size on disk / eligible to reclaim","on_surface_variant",11))
    for _,g in ipairs(data.groups) do
      table.insert(body,ui.row({gap=12,align="center"},{
        ui.toggle({checked=g.enabled,enabled=not busy,onChange=function(v) toggle(g.id,v) end}),
        ui.column({gap=5,width=648},{
          ui.row({justify="space_between"},{text(g.label),text(size(g.bytes).." / "..size(g.eligible),"primary",12)}),
          ui.progress({height=5,progress=g.bytes>0 and math.min(1,g.eligible/g.bytes) or 0,fill="primary",track="surface_variant"})
        })
      }))
    end
    table.insert(body,text("Trash can contain unique files. It is excluded until you select it.","on_surface_variant",12))
    table.insert(body,text("Active items are skipped. Shared hard links are counted once; external links reduce reclaim estimates.","on_surface_variant",12))
  elseif tab=="disks" then
    for _,d in ipairs(data.disks) do
      table.insert(body,ui.column({gap=5},{
        ui.row({justify="space_between"},{text(d.path),text(size(d.used).." used · "..size(d.free).." free","on_surface_variant",12)}),
        ui.progress({height=10,progress=d.used/d.total,fill=d.free/d.total<0.1 and "error" or "primary",track="surface_variant"})
      }))
    end
    table.insert(body,button("Scan every storage mount",function() run("all-mounts") end,"secondary"))
    table.insert(body,text("DIRECTORY EXPLORER","on_surface_variant",11))
    local result=data.explorer.results and data.explorer.results[1]
    if result then
      table.insert(body,text(short(result.path),"primary",15))
      if result.path~="/" then
        local parent=string.match(result.path,"^(.*)/[^/]+$");if parent=="" then parent="/" end
        table.insert(body,button("↑ Parent",function() run("explore"," --path "..quote(parent)) end))
      end
      for _,row in ipairs(result.rows) do
        if row.path~=result.path then
          local path=row.path
          table.insert(body,button(size(row.bytes).."  "..short(path),function() run("explore"," --path "..quote(path)) end))
        end
      end
      if result.partial then table.insert(body,text(short(result.note),"error",12)) end
    end
    table.insert(body,text("MOUNTS","on_surface_variant",11))
    for _,m in ipairs(data.mounts) do
      if m.scan then local p=m.target;table.insert(body,button(short(p).." · "..m.fstype,function() run("explore"," --path "..quote(p)) end)) end
    end
  elseif tab=="files" then
    table.insert(body,text("Largest candidates · exact paths and protection reasons","on_surface_variant",11))
    table.insert(body,ui.row({gap=8},{button("Previous",function() page=math.max(1,page-1);render() end),text("Page "..page),button("Next",function() page=math.min(math.ceil(#data.items/12),page+1);render() end)}))
    for i=(page-1)*12+1,math.min(page*12,#data.items) do
      local item=data.items[i]
      table.insert(body,ui.column({padding=8,gap=4,fill="surface_variant",radius=8},{
        text(short(item.path),"on_surface",12),text(size(item.bytes).." · "..(item.eligible and "Ready" or item.reason),item.eligible and "primary" or "on_surface_variant",12)
      }))
    end
  elseif tab=="system" then
    local sys=data.system or {}
    table.insert(body,text("System maintenance","primary",18))
    table.insert(body,text("Changes here request administrator authorization in a graphical dialog.","on_surface_variant",12))
    table.insert(body,text("Pacman cache · "..size(sys.package_bytes or 0),"primary",16))
    table.insert(body,text(sys.package_preview or "Refresh system preview", "on_surface_variant",12))
    table.insert(body,button("Prune package cache · keep newest version",function() run("system"," --kind packages") end,"secondary"))
    table.insert(body,button("Empty ALL downloaded packages · authorize",function() run("system"," --kind packages-all") end,"destructive"))
    table.insert(body,text("Btrfs snapshots · "..tostring(sys.snapshot_count or 0),"primary",16))
    table.insert(body,text("Remove "..tostring(#(sys.snapshot_remove or {})).." older snapshots; retain the newest two pre/post pairs. Shared extents mean snapshot sizes do not equal freed space.","on_surface_variant",12))
    table.insert(body,button(adminConfirm=="snapshots" and "Confirm snapshot removal · authorize" or "Remove old snapshots…",function()
      if adminConfirm=="snapshots" then adminConfirm=nil;run("system"," --kind snapshots") else adminConfirm="snapshots";render() end
    end,"destructive"))
    table.insert(body,button("Clean old system temporary files · authorize",function() run("system"," --kind temp") end,"secondary"))
    if data.vm and data.vm.bytes then
      table.insert(body,text("VM budget · "..size(data.vm.bytes).." / 120 GB", "primary",16))
      table.insert(body,text("Reims macOS + Windows 11 only. Required emulators, firmware and drivers are retained.","on_surface_variant",12))
    end
  elseif tab=="settings" then
    table.insert(body,text("Minimum time since modification","primary",16))
    local ages={}
    for _,age in ipairs({0,1,7,30,90}) do table.insert(ages,button(age==0 and "Any age" or age.." days",function() run("configure"," --age "..age) end,data.config.age_days==age and "primary" or "ghost")) end
    table.insert(body,ui.row({gap=6},ages))
    table.insert(body,text("Automatic cleanup","primary",16))
    local times={}
    for i,days in ipairs({0,1,7,30}) do
      table.insert(times,button(({"Manual","Daily","Weekly","Monthly"})[i],function() run("configure"," --schedule "..days) end,data.config.schedule_days==days and "primary" or "ghost"))
    end
    table.insert(body,ui.row({gap=6},times))
    table.insert(body,ui.row({gap=6},{button("Permanent deletion",function() run("configure"," --mode delete") end,data.config.mode=="delete" and "destructive" or "ghost"),button("Recovery + 7 days",function() run("configure"," --mode stage") end,data.config.mode=="stage" and "primary" or "ghost")}))
    table.insert(body,text(data.config.schedule_days==0 and "Automatic cleanup is off." or "Runs on the chosen interval using selected categories and age. Permanent deletion cannot be undone.","on_surface_variant",12))
    table.insert(body,text("Recovery · "..size(data.recovery_bytes),"primary",16))
    table.insert(body,button("Restore recovery items",function() run("restore") end,"secondary",data.recovery_count>0))
    table.insert(body,button("Permanently empty recovery",function() if message=="Confirm empty recovery" then run("purge") else message="Confirm empty recovery";render() end end,"destructive",data.recovery_count>0))
    if data.last_action.errors then for _,e in ipairs(data.last_action.errors) do table.insert(body,text(short(e),"error",12)) end end
    table.insert(body,text("Create .keepstorage or .keepbuild in a directory to exclude its entire subtree. Future file use cannot be predicted by a process snapshot.","on_surface_variant",12))
  end
  local tabs={}
  for _,t in ipairs({{"clean","Clean"},{"disks","All disks"},{"files","Files"},{"system","System"},{"settings","Schedule & recovery"}}) do
    table.insert(tabs,button(t[2],function() setTab(t[1]) end,tab==t[1] and "secondary" or "ghost"))
  end
  panel.render(ui.column({padding=16,gap=10,width=760},{
    ui.row({justify="space_between",align="center"},{text("Smart Storage","primary",23),ui.button({glyph="close",variant="ghost",onClick=function() panel.close() end})}),
    ui.row({gap=10},{metric("READY TO RECLAIM",data and size(data.eligible) or "—"),metric("CLEANUP AGE",data and (data.config.age_days==0 and "Any age" or data.config.age_days.." days") or "—"),metric("AUTOMATION",data and (data.config.schedule_days==0 and "Off" or "Every "..data.config.schedule_days.."d") or "—")}),
    ui.row({gap=6},tabs),
    ui.scroll({height=320,width=728},{ui.column({width=700,gap=12},body)}),
    text(short(message),"on_surface_variant",12),
    ui.row({gap=8},{button(busy and "Working…" or "Rescan",function() run("scan") end,"secondary"),button("Delete selected…",function() confirmation=true;render() end,"destructive",data and data.eligible>0),button("Move to recovery",function() run("stage") end,"ghost",data and data.eligible>0),button("Full report",function() noctalia.runAsync("xdg-open "..quote(noctalia.expandPath("~/.local/state/smart-storage/report.json"))) end)})
  }))
end
function onOpen() panel.setWantsSecondTicks(true);refresh() end
function update() if busy then refresh() end end
function onIpc(event,payload)
  if event=="scan" then run("scan") elseif event=="tab" then setTab(payload) else refresh() end
end
