local data, render, refresh, run
local busy, polling, lastStatus, pending, hidden = false, false, "", false, {}
-- open: expanded category · focus: disk path the list is narrowed to · hover: what the pointer is on (readout + highlight)
local view, open, focus, hover, message, confirm = "items", nil, nil, nil, "", nil
local HOME = noctalia.expandPath("~")
local function quote(s) return "'"..string.gsub(s,"'","'\\''").."'" end
local function cleaner(args) return "python3 "..quote(noctalia.pluginDir().."/cleaner.py").." "..args end
local function size(n)
  if not n or n<=0 then return "—" end
  if n>=1073741824 then return string.format("%.1f G",n/1073741824) end
  if n>=1048576 then return string.format("%.0f M",n/1048576) end
  return string.format("%.0f K",n/1024)
end
local function short(s,n)
  s=string.gsub(s,"^"..string.gsub(HOME,"%p","%%%0"),"~")
  n=n or 70
  return #s>n and "…"..string.sub(s,-(n-1)) or s
end
local function text(s,color,font,extra)
  local o={text=s,color=color or "on_surface",fontSize=font or 12,maxLines=2}
  if extra then for k,v in pairs(extra) do o[k]=v end end
  return ui.label(o)
end
local function button(label,fn,variant,enabled,extra)
  local o={text=label,onClick=fn,variant=variant or "ghost",enabled=enabled~=false,controlSize="sm"}
  if extra then for k,v in pairs(extra) do o[k]=v end end
  return ui.button(o)
end
local function mutate(label,fn,variant,enabled) return button(label,fn,variant,enabled~=false and not busy) end
local function icon(glyph,tip,fn,enabled,selected)
  return ui.button({glyph=glyph,tooltip=tip,onClick=fn,variant=selected and "secondary" or "ghost",controlSize="sm",enabled=enabled~=false})
end
local function alpha(c,a) return string.sub(c,1,1)=="#" and c..string.format("%02x",math.floor(a*255)) or c.."/"..a end
local WHY={{"^In use","in use"},{"^Protected while","tool running"},{"^Project is in use","project open"},{"special files","special files"},
  {"^Uncommitted","uncommitted"},{"^Not merged","unmerged"},{"^Checked out","checked out"},{"^Recently","changed recently"},
  {"^Unreadable","unreadable"},{"^Not entirely owned","not yours"},{"mount","has a mount"},{"keep marker","kept"},
  {"^Locked","locked"},{"^Symlink","symlink"},{"^Process visibility","can't verify"}}
local function why(r)
  for _,w in ipairs(WHY) do if string.find(r,w[1]) then return w[2] end end
  return string.lower(r)
end
local function diskName(d) return d.ram and "/tmp (RAM)" or d.path=="/" and "/" or string.match(d.path,"[^/]+$") end
local function group(id) for _,g in ipairs(data.groups) do if g.id==id then return g end end end
local function systemBytes() local s=data.system or {} return (s.package_bytes or 0)+(s.journal_bytes or 0) end

-- Status polling: only re-render when the backend output actually changed.
refresh=function()
  if polling then return end
  polling=true
  noctalia.runAsync(cleaner("status"),function(r)
    polling=false
    local out=r.stdout or ""
    if out==lastStatus then return end
    lastStatus=out
    local d=noctalia.json.decode(out)
    if r.exitCode~=0 or not d or d.error then message=d and d.error or "Status unavailable"
    else
      data=d;busy=d.job and d.job.running or false;pending=(d.pending or 0)>0
      if not busy then hidden={} end  -- the finished job's report no longer has what it removed
      message=busy and ("Running "..d.job.operation.."… you can close this panel") or ""
      if not busy and d.job and d.job.error and d.job.error~="" then message=d.job.error end
      if not busy and d.last_action and d.last_action.at and d.last_action.at>(d.at or 0)-60 then
        local a=d.last_action
        message=string.format("Last run freed %s · %d removed, %d staged, %d skipped",size(a.bytes),a.deleted or 0,a.staged or 0,a.skipped or 0)
        if a.errors and #a.errors>0 then message=message.." · "..#a.errors.." errors (Settings)" end
      end
      if not busy and pending then message="Removing "..d.pending.." items in the background…" end
    end
    render()
  end)
end
-- Background job (scan/system/restore/purge): the UI stays interactive while it runs.
run=function(action,extra)
  if busy then return end
  busy=true;confirm=nil;message="Starting "..action.."…";render()
  noctalia.runAsync(cleaner("start --operation "..action..(extra or "")),function(r)
    local d=noctalia.json.decode(r.stdout or "")
    if r.exitCode~=0 then busy=false;hidden={};message=d and d.error or "Could not start";render() else lastStatus="";refresh() end
  end)
end
-- Settings changes are instant: local state first, then a direct (non-job) configure.
local function configure(args,apply)
  apply(data.config)
  render()
  noctalia.runAsync(cleaner("configure "..args),function(r)
    local d=noctalia.json.decode(r.stdout or "")
    if r.exitCode~=0 then message=d and d.error or "Could not save settings";render() end
    lastStatus="";refresh()
  end)
end
local function toggleCategory(g,on)
  local ids={}
  for _,x in ipairs(data.groups) do
    if x.id==g.id then x.enabled=on end
    if x.enabled then table.insert(ids,x.id) end
  end
  configure("--categories "..quote(table.concat(ids,",")),function(c) c.categories=ids end)
end
-- Deleting is a detached job, so closing the panel never cuts it short: rows vanish at once, the finished job's
-- report brings the real state (anything it skipped comes back), and the reaper frees the space behind it.
local function remove(id)
  if busy then return end
  local on={}
  for _,g in ipairs(data.groups) do on[g.id]=g.enabled end
  for _,x in ipairs(data.items) do
    if x.id==id or (not id and x.eligible and on[x.category]) then hidden[x.id]=true end
  end
  run(data.config.mode=="delete" and "delete" or "stage",id and " --ids "..id or "")
end
local function selectedTotal()
  local n=0
  for _,g in ipairs(data.groups) do if g.enabled then n=n+g.eligible end end
  return n
end
-- Pointer tracking: every hover target has its own key, so leaving one after entering the next never clears the new one.
local function hoverOn(h)
  return function(state)
    if state=="true" or state==true then hover=h;render()
    elseif hover and hover.key==h.key then hover=nil;render() end
  end
end

-- One track per disk, to scale: everything else used | held back | ready | selected, then free.
-- What is selected sits against the free space, so the bar shows exactly how far free space will grow.
local function track(d)
  local held,idle,sel,sum={},{},{},0
  for _,g in ipairs(data.groups) do
    local v=d.cats[g.id]
    if v and v[1]>0 then
      sum=sum+v[1]
      if v[1]>v[2] then table.insert(held,{g,v[1]-v[2],alpha(g.color,0.22),"held"}) end
      if v[2]>0 then table.insert(g.enabled and sel or idle,{g,v[2],g.enabled and g.color or alpha(g.color,0.5),"ready"}) end
    end
  end
  local sys=d.path=="/" and systemBytes() or 0
  local scale=sum+sys>d.used and d.used/(sum+sys) or 1  -- apparent sizes can exceed allocation (compression, reflinks)
  local parts={}
  local function add(key,bytes,fill,cat,click,min)
    if bytes<=0 then return end
    local h={key=d.path.."|"..key,cat=cat,disk=d}
    table.insert(parts,ui.row({key=h.key,flexGrow=bytes/d.total,minWidth=min or 0,height=10,radius=3,fill=fill,
      opacity=(hover and hover.cat and cat~=hover.cat) and 0.25 or 1,onHover=hoverOn(h),onClick=click},{}))
  end
  add("other",d.used-(sum+sys)*scale,"on_surface/0.16")
  add("system",sys*scale,"on_surface/0.4","system",function() open="system";focus=nil;view="items";render() end,3)
  for _,list in ipairs({held,idle,sel}) do
    for _,s in ipairs(list) do
      local g=s[1]
      add(g.id.."|"..s[4],s[2]*scale,s[3],g.id,function() open=g.id;focus=d.path;view="items";render() end,3)
    end
  end
  table.insert(parts,ui.row({key=d.path.."|free",flexGrow=math.max(0,d.free/d.total),height=10,onHover=hoverOn({key=d.path.."|free",disk=d})},{}))
  return ui.row({gap=2,height=10,flexGrow=1,radius=3,fill="surface_variant"},parts)
end

local function diskRow(d)
  local ready=0
  for _,g in ipairs(data.groups) do local v=d.cats[g.id]; if v and g.enabled then ready=ready+v[2] end end
  local focused=focus==d.path
  local pick=function() focus=(not focused) and d.path or nil;view="items";render() end
  return ui.row({key=d.path,gap=10,align="center"},{
    ui.row({width=92,onClick=pick,onHover=hoverOn({key=d.path.."|name",disk=d})},{
      text(diskName(d),focused and "primary" or "on_surface",12,{maxLines=1,fontWeight=focused and "semibold" or nil})}),
    track(d),
    ui.row({width=104,gap=4,justify="end",align="center",onClick=pick,onHover=hoverOn({key=d.path.."|num",disk=d})},{
      text(size(d.free),d.free/d.total<0.1 and "error" or "on_surface",12),
      text(ready>0 and ("+"..size(ready)) or "","primary",11)})})
end

local function readout()
  if hover and hover.cat=="system" then return "Pacman cache and journal · cleaned with admin rights, click to open" end
  if hover and hover.cat then
    local g=group(hover.cat)
    local v=hover.disk and hover.disk.cats[hover.cat] or {g.bytes,g.eligible}
    local s=g.label..(hover.disk and (" on "..diskName(hover.disk)) or "").." · "..size(v[2]).." ready"
    if v[1]>v[2] then s=s.." · "..size(v[1]-v[2]).." held"..(g.why~="" and (", mostly "..why(g.why)) or "") end
    return s..(g.enabled and "" or " · not selected")
  end
  if hover and hover.disk then
    local d=hover.disk
    return (d.ram and "RAM-backed /tmp" or d.path.." ("..d.source..")").." · "..size(d.used).." of "..size(d.total).." used · "..size(d.free).." free"
  end
  if message~="" then return message end
  local free,n=0,0
  for _,d in ipairs(data.disks) do if not d.ram then free=free+d.free;n=n+1 end end
  return size(free).." free across "..n.." disks · click a bar to see what's in it"
end

local function catRow(g,v,count)
  local expanded=open==g.id
  local heldBytes=v[1]-v[2]
  local amount=g.id=="branches" and (count.." branches") or size(v[2])
  return ui.row({key="cat|"..g.id,gap=8,align="center",paddingH=6,paddingV=3,radius=6,
    fill=(expanded or (hover and hover.cat==g.id)) and "surface_variant" or nil},{
    ui.row({width=12,height=12,radius=3,fill=g.enabled and g.color or nil,border=g.color,borderWidth=2,
      tooltip=g.enabled and "Selected · click to leave out of Delete" or "Click to include in Delete",
      onClick=function() toggleCategory(g,not g.enabled) end},{}),
    ui.row({flexGrow=1,gap=8,align="center",onHover=hoverOn({key="cat|"..g.id,cat=g.id}),
      onClick=function() open=(not expanded) and g.id or nil;confirm=nil;render() end},{
      text(g.label,v[2]>0 and "on_surface" or "on_surface_variant",12,{flexGrow=1,maxLines=1}),
      text(heldBytes>0 and (size(heldBytes).." "..why(g.why)) or "","on_surface_variant",11,{maxLines=1}),
      ui.row({width=64,justify="end"},{text(amount,v[2]>0 and "primary" or "on_surface_variant",12)}),
      ui.glyph({name=expanded and "chevron-down" or "chevron-right",size=14,color="on_surface_variant"})})})
end

local function itemRow(x)
  local sure=confirm==x.id
  local isBranch=x.category=="branches"
  local tip=x.path..(x.detail and x.detail~="" and ("\n"..x.detail) or "")..(x.reason~="" and ("\n"..x.reason) or "")
  return ui.row({key=x.id,gap=8,align="center",paddingH=6,radius=6,tooltip=tip},{
    ui.row({width=12},{}),
    text(isBranch and string.gsub(x.path,"^.*#","") or short(x.path,64),x.eligible and "on_surface" or "on_surface_variant",11,{flexGrow=1,maxLines=1}),
    text(x.eligible and "" or why(x.reason),"on_surface_variant",11,{maxLines=1}),
    ui.row({width=64,justify="end"},{text(isBranch and "" or size(x.bytes),"on_surface_variant",11)}),
    ui.button({glyph=not sure and "trash" or nil,text=sure and "Sure?" or nil,variant=sure and "destructive" or "ghost",controlSize="sm",
      tooltip=isBranch and "git branch -d" or (data.config.mode=="delete" and "Delete" or "Move to recovery folder"),
      enabled=x.eligible and not busy,onClick=function() if sure then remove(x.id) else confirm=x.id;render() end end})})
end

local function systemRows()
  local s=data.system or {}
  local rows={}
  local function kv(k,v,...)
    local r={text(k,"on_surface",11,{maxLines=1}),text(v or "","on_surface_variant",11,{flexGrow=1,maxLines=1})}
    for _,b in ipairs({...}) do table.insert(r,b) end
    table.insert(rows,ui.row({gap=8,align="center",paddingH=6},{ui.row({width=12},{}),ui.row({gap=8,align="center",flexGrow=1},r)}))
  end
  local n,saved=string.match(s.package_preview or "","(%d+) candidates.-saved: ([%d%.]+ %a+)")
  kv("Pacman cache "..size(s.package_bytes),n and ("keeping newest frees "..saved..", "..n.." packages") or "",
    mutate("Keep newest",function() run("system"," --kind packages") end,"secondary"),
    mutate(confirm=="packages-all" and "Sure?" or "Empty",function() if confirm=="packages-all" then run("system"," --kind packages-all") else confirm="packages-all";render() end end,"destructive"))
  kv("Btrfs snapshots "..tostring(s.snapshot_count or 0),#(s.snapshot_remove or {}).." old, keeps newest pairs",
    mutate(confirm=="snapshots" and "Sure?" or "Remove old",function() if confirm=="snapshots" then run("system"," --kind snapshots") else confirm="snapshots";render() end end,"destructive",#(s.snapshot_remove or {})>0))
  kv("Journal "..size(s.journal_bytes),s.journal_bytes and "to 200 M / 14 days" or "update the helper (README)",
    mutate("Vacuum",function() run("system"," --kind journal") end,"secondary",s.journal_bytes~=nil))
  kv("/tmp, /var/tmp","systemd-tmpfiles, skips files in use",mutate("Clean",function() run("system"," --kind temp") end,"secondary"))
  return rows
end

local function items()
  local d
  for _,x in ipairs(data.disks) do if x.path==focus then d=x end end
  local counts,shown={},{}
  for _,x in ipairs(data.items) do
    if not focus or x.disk==focus then
      counts[x.category]=(counts[x.category] or 0)+1
      if x.category==open and not hidden[x.id] then table.insert(shown,x) end
    end
  end
  local cats={}
  for _,g in ipairs(data.groups) do
    local v=d and d.cats[g.id] or (g.count>0 and {g.bytes,g.eligible})
    if v then table.insert(cats,{g,v}) end
  end
  table.sort(cats,function(a,b) if a[2][2]~=b[2][2] then return a[2][2]>b[2][2] end return a[2][1]>b[2][1] end)
  local rows={}
  for _,c in ipairs(cats) do
    local g=c[1]
    table.insert(rows,catRow(g,c[2],d and counts[g.id] or g.count))
    if open==g.id then
      for _,x in ipairs(shown) do table.insert(rows,itemRow(x)) end
      local more=(d and counts[g.id] or g.count)-#shown
      if more>0 and not d then table.insert(rows,text("   +"..more.." smaller, not listed","on_surface_variant",11)) end
    end
  end
  if not d or d.path=="/" then
    local expanded=open=="system"
    table.insert(rows,ui.row({key="cat|system",gap=8,align="center",paddingH=6,paddingV=3,radius=6,fill=expanded and "surface_variant" or nil,
      onHover=hoverOn({key="cat|system",cat="system"}),onClick=function() open=(not expanded) and "system" or nil;confirm=nil;render() end},{
      ui.glyph({name="shield-lock",size=12,color="on_surface_variant"}),
      text("System (admin)","on_surface",12,{flexGrow=1}),
      ui.row({width=64,justify="end"},{text(size(systemBytes()),"on_surface_variant",12)}),
      ui.glyph({name=expanded and "chevron-down" or "chevron-right",size=14,color="on_surface_variant"})}))
    if expanded then for _,r in ipairs(systemRows()) do table.insert(rows,r) end end
  end
  if #rows==0 then table.insert(rows,text("Nothing found here.","on_surface_variant")) end
  return rows
end

local function settings()
  local c=data.config
  local rows={}
  local function choices(label,opts,current,fn)
    local b={}
    for _,o in ipairs(opts) do table.insert(b,button(o[2],function() fn(o[1]) end,current==o[1] and "primary" or "ghost")) end
    table.insert(rows,ui.row({gap=8,align="center"},{ui.column({width=150},{text(label,"on_surface",12)}),ui.row({gap=4},b)}))
  end
  choices("Removal",{{"delete","Permanent"},{"stage","Recovery folder, 7 days"}},c.mode,function(v) configure("--mode "..v,function(cc) cc.mode=v end) end)
  choices("Skip if changed within",{{0,"never"},{1,"1 d"},{7,"7 d"},{30,"30 d"},{90,"90 d"}},c.age_days,function(v) configure("--age "..v,function(cc) cc.age_days=v end) end)
  choices("Automatic run",{{0,"off"},{1,"daily"},{7,"weekly"},{30,"monthly"}},c.schedule_days,function(v) configure("--schedule "..v,function(cc) cc.schedule_days=v end) end)
  table.insert(rows,ui.row({gap=8,align="center"},{text("Recovery folder · "..size(data.recovery_bytes).." · "..data.recovery_count.." items","on_surface",12,{flexGrow=1}),
    mutate("Restore",function() run("restore") end,"secondary",data.recovery_count>0),
    mutate(confirm=="purge" and "Sure?" or "Empty",function() if confirm=="purge" then run("purge") else confirm="purge";render() end end,"destructive",data.recovery_count>0)}))
  for _,e in ipairs(data.last_action and data.last_action.errors or {}) do table.insert(rows,text(short(e,120),"error",11)) end
  table.insert(rows,text("A .keepstorage file protects its folder. Worktrees are removed and pruned (never staged, locked ones kept); branches use git branch -d.","on_surface_variant",11,{maxLines=3}))
  return rows
end

render=function()
  if not data then panel.render(ui.column({padding=14,gap=10},{text("Loading storage…")}));return end
  local total=selectedTotal()
  local tracks={}
  for _,d in ipairs(data.disks) do table.insert(tracks,diskRow(d)) end
  local line={text(readout(),"on_surface_variant",11,{flexGrow=1,maxLines=1})}
  if focus then table.insert(line,button("Only "..string.match(focus,"[^/]*$").." ×",function() focus=nil;render() end,"ghost")) end
  panel.render(ui.column({flexGrow=1,padding=14,gap=10},{
    ui.row({gap=6,align="center"},{
      text("Storage","primary",15,{flexGrow=1,fontWeight="semibold"}),
      mutate(confirm=="all" and ("Sure? "..size(total)) or ((data.config.mode=="delete" and "Delete " or "Stage ")..size(total)),
        function() if confirm=="all" then remove(nil) else confirm="all";render() end end,"destructive",total>0),
      icon("refresh",busy and "Working…" or "Rescan",function() run("scan") end,not busy),
      icon("settings","Settings",function() view=view=="settings" and "items" or "settings";confirm=nil;render() end,true,view=="settings"),
      icon("x","Close",function() panel.close() end)}),
    ui.column({gap=5},tracks),
    ui.row({gap=6,align="center",height=22},line),
    ui.scroll({flexGrow=1},{ui.column({gap=1},view=="settings" and settings() or items())})}))
end
function onOpen() panel.setWantsSecondTicks(true);lastStatus="";refresh() end
function update() if busy or pending then refresh() end end
function onIpc(event,payload)
  if event=="scan" then run("scan") elseif event=="view" then view=payload;render() else lastStatus="";refresh() end
end
