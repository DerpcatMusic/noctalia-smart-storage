local data, render, refresh, run
local busy, polling, lastStatus = false, false, ""
local view, filter, message, confirm = "items", nil, "", nil
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
      data=d;busy=d.job and d.job.running or false
      message=busy and ("Running "..d.job.operation.."… you can close this panel.") or ""
      if not busy and d.job and d.job.error and d.job.error~="" then message=d.job.error end
      if not busy and d.last_action and d.last_action.at and d.last_action.at>(d.at or 0)-60 then
        local a=d.last_action
        message=string.format("Last run: %d removed, %d staged, %d skipped · %s freed",a.deleted or 0,a.staged or 0,a.skipped or 0,size(a.bytes))
        if a.errors and #a.errors>0 then message=message.." · "..#a.errors.." errors (see Settings)" end
      end
    end
    render()
  end)
end
-- Background job (scan/delete/system): the UI stays interactive while it runs.
run=function(action,extra)
  if busy then return end
  busy=true;confirm=nil;message="Starting "..action.."…";render()
  noctalia.runAsync(cleaner("start --operation "..action..(extra or "")),function(r)
    local d=noctalia.json.decode(r.stdout or "")
    if r.exitCode~=0 then busy=false;message=d and d.error or "Could not start";render() else lastStatus="";refresh() end
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
local function eligibleTotal()
  local n=0
  for _,g in ipairs(data.groups) do if g.enabled then n=n+g.eligible end end
  return n
end

local function header()
  local root, others = data.disks[1], {}
  for _,d in ipairs(data.disks) do
    if d.path=="/" then root=d elseif #others<3 then table.insert(others,short(d.path,14).." "..size(d.free)) end
  end
  local low=root.free/root.total<0.1
  return ui.column({gap=4},{
    ui.row({gap=12,align="center"},{text("Smart Storage","primary",18),
      text(size(root.free).." free of "..size(root.total),low and "error" or "on_surface",12,{flexGrow=1,textAlign="end"}),
      ui.button({glyph="close",variant="ghost",controlSize="sm",onClick=function() panel.close() end})}),
    ui.progress({height=4,progress=root.used/root.total,fill=low and "error" or "primary",track="surface_variant"}),
    #others>0 and text("other mounts: "..table.concat(others,"  ·  "),"on_surface_variant",10,{maxLines=1}) or nil})
end

local function overview()
  local legend={}
  for _,g in ipairs(data.groups) do
    if g.count>0 then
      local selected=filter==g.id
      local amount=size(g.bytes)
      if g.eligible>0 and g.eligible<g.bytes*0.98 then amount=amount.." · "..size(g.eligible).." free" end
      if g.id=="branches" then amount=g.count.." branches" end
      table.insert(legend,ui.row({gap=8,align="center",paddingH=6,paddingV=1,radius=6,fill=selected and "surface_variant" or nil,
        onClick=function() filter=(selected and nil or g.id);view="items";render() end},{
        ui.column({width=8,height=8,radius=4,fill=g.color},{}),
        ui.toggle({checked=g.enabled,onChange=function(v) toggleCategory(g,v==true or v=="true") end}),
        text(g.label,selected and "primary" or "on_surface",12,{flexGrow=1,maxLines=1}),
        text(amount,g.eligible>0 and "primary" or "on_surface_variant",11)}))
    end
  end
  local pie=data.pie and data.pie~="" and ui.image({path=data.pie,width=150,height=150,fit="contain"}) or ui.column({width=150,height=150},{text("Nothing found","on_surface_variant")})
  return ui.row({gap=14,align="center"},{
    ui.column({gap=4,align="center"},{pie,text(size(eligibleTotal()).." selected","primary",14),text("click a category to filter","on_surface_variant",10)}),
    ui.column({gap=1,flexGrow=1},legend)})
end

local function itemRow(x)
  local sure=confirm==x.id
  local isBranch=x.category=="branches"
  local name=isBranch and string.gsub(x.path,"^.*#","") or short(x.path)
  return ui.column({key=x.id,gap=0,paddingH=8,paddingV=4,radius=8,fill="surface_variant"},{
    ui.row({gap=8,align="center"},{
      text(name,x.eligible and "on_surface" or "on_surface_variant",12,{flexGrow=1,maxLines=1}),
      text(isBranch and "branch" or size(x.bytes),"primary",12),
      button(sure and "Sure?" or (isBranch and "git branch -d" or (data.config.mode=="delete" and "Delete" or "Stage")),
        function() if sure then run(data.config.mode=="delete" and "delete" or "stage"," --ids "..x.id) else confirm=x.id;render() end end,
        sure and "destructive" or "ghost",x.eligible and not busy)}),
    text((x.reason~="" and x.reason.."  ·  " or "")..(x.detail or ""),x.reason~="" and "error" or "on_surface_variant",11,{maxLines=1})})
end

local function items()
  local rows,n,shown={},0,0
  for _,x in ipairs(data.items) do
    if not filter or x.category==filter then
      n=n+1
      if shown<80 then table.insert(rows,itemRow(x));shown=shown+1 end
    end
  end
  if n==0 then table.insert(rows,text("Nothing in this category.","on_surface_variant")) end
  if n>shown then table.insert(rows,text(n-shown.." more · narrow the filter","on_surface_variant",11)) end
  return rows
end

local function system()
  local s=data.system or {}
  local rows={text("System (asks for administrator authorization)","primary",14)}
  local function kv(k,v,b) table.insert(rows,ui.row({gap=8,align="center"},{text(k,"on_surface",12,{flexGrow=1}),text(v,"on_surface_variant",11,{flexGrow=2}),b})) end
  kv("Pacman package cache · "..size(s.package_bytes),s.package_preview or "no preview",mutate("Keep newest only",function() run("system"," --kind packages") end,"secondary"))
  kv("",  "Empty every downloaded package",mutate("Empty all",function() run("system"," --kind packages-all") end,"destructive"))
  kv("Btrfs snapshots · "..tostring(s.snapshot_count or 0),"remove "..#(s.snapshot_remove or {}).." old, keep newest pairs",
     mutate(confirm=="snapshots" and "Sure?" or "Remove old",function() if confirm=="snapshots" then run("system"," --kind snapshots") else confirm="snapshots";render() end end,"destructive"))
  kv("System journal · "..size(s.journal_bytes),s.journal_bytes and ("/var/log "..size(s.log_bytes)) or "helper not updated: run the install command from the README",mutate("Vacuum to 200 M / 14 d",function() run("system"," --kind journal") end,"secondary",s.journal_bytes~=nil))
  kv("/tmp and /var/tmp","systemd-tmpfiles --clean, skips files in use",mutate("Clean",function() run("system"," --kind temp") end,"secondary"))
  return rows
end

local function settings()
  local c=data.config
  local rows={text("Settings","primary",14)}
  local function choices(label,opts,current,fn)
    local b={}
    for _,o in ipairs(opts) do table.insert(b,button(o[2],function() fn(o[1]) end,current==o[1] and "primary" or "ghost")) end
    table.insert(rows,ui.row({gap=8,align="center"},{ui.column({minWidth=150},{text(label,"on_surface",12)}),ui.row({gap=4},b)}))
  end
  choices("Removal",{{"delete","Permanent"},{"stage","Recovery folder (7 days)"}},c.mode,function(v) configure("--mode "..v,function(cc) cc.mode=v end) end)
  choices("Skip if changed within",{{0,"never"},{1,"1 day"},{7,"7 days"},{30,"30 days"},{90,"90 days"}},c.age_days,function(v) configure("--age "..v,function(cc) cc.age_days=v end) end)
  choices("Automatic run",{{0,"off"},{1,"daily"},{7,"weekly"},{30,"monthly"}},c.schedule_days,function(v) configure("--schedule "..v,function(cc) cc.schedule_days=v end) end)
  table.insert(rows,ui.row({gap=8,align="center"},{text("Recovery folder · "..size(data.recovery_bytes).." · "..data.recovery_count.." items","on_surface",12,{flexGrow=1}),
    mutate("Restore",function() run("restore") end,"secondary",data.recovery_count>0),
    mutate(confirm=="purge" and "Sure?" or "Empty",function() if confirm=="purge" then run("purge") else confirm="purge";render() end end,"destructive",data.recovery_count>0)}))
  for _,e in ipairs(data.last_action and data.last_action.errors or {}) do table.insert(rows,text(short(e,120),"error",11)) end
  table.insert(rows,text("Worktrees are removed with git worktree remove (never staged); branches with git branch -d (merged only). Create .keepstorage in a folder to protect its subtree.","on_surface_variant",11,{maxLines=3}))
  return rows
end

render=function()
  if not data then panel.render(ui.column({padding=14,gap=10},{text("Loading storage…")}));return end
  local body=view=="system" and system() or view=="settings" and settings() or items()
  local total=eligibleTotal()
  local filterLabel=filter and (" · "..filter) or ""
  panel.render(ui.column({flexGrow=1,padding=14,gap=10},{
    header(),
    overview(),
    ui.row({gap=6,align="center"},{
      mutate(busy and "Working…" or "Rescan",function() run("scan") end,"secondary"),
      mutate(confirm=="all" and ("Sure? "..size(total)) or ((data.config.mode=="delete" and "Delete selected " or "Stage selected ")..size(total)),
        function() if confirm=="all" then run(data.config.mode=="delete" and "delete" or "stage") else confirm="all";render() end end,"destructive",total>0),
      text(message,"on_surface_variant",11,{flexGrow=1,maxLines=1}),
      button("Items"..filterLabel,function() view="items";render() end,view=="items" and "secondary" or "ghost"),
      button("System",function() view="system";confirm=nil;render() end,view=="system" and "secondary" or "ghost"),
      button("Settings",function() view="settings";confirm=nil;render() end,view=="settings" and "secondary" or "ghost")}),
    ui.scroll({flexGrow=1},{ui.column({gap=4},body)})}))
end
function onOpen() panel.setWantsSecondTicks(true);lastStatus="";refresh() end
function update() if busy then refresh() end end
function onIpc(event,payload)
  if event=="scan" then run("scan") elseif event=="view" then view=payload;render() else lastStatus="";refresh() end
end
