"use strict";
const $=id=>document.getElementById(id);
let selected=null, job=null, eventOffset=0, eventTotal=0, signalPoints=[], positionPoints=[], cursor=null, chartRevision=0,eventRevision=0,parameterRevision=0;
const pageSize=100;
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(className)node.className=className;return node;}
function error(message){$("error").textContent=message;$("error").hidden=false;}
async function api(url,options){
 const response=await fetch(url,options),text=await response.text();let value;
 try{value=JSON.parse(text);}catch{throw new Error(`HTTP ${response.status}: ${text.slice(0,200)||"Empty server response"}`);}
 if(!response.ok)throw new Error(typeof value.detail==="string"?value.detail:JSON.stringify(value.detail));return value;
}
function run(action){return (...args)=>Promise.resolve(action(...args)).catch(e=>error(e.message));}
function timeLabel(value){if(value===null||value===undefined)return "No timestamp";return job&&["bin","log"].includes(job.format)?`boot +${(value/1e6).toFixed(3)}s`:new Date(value/1000).toISOString();}
function base(){return `/api/logs/${selected}`;}
function filters(){return new URLSearchParams({source:$("source").value,epoch:$("epoch").value});}

async function refreshProfiles(){
 const profiles=await api("/api/profiles");const chosen=$("profile").value;
 $("profile").replaceChildren(element("option","No profile — generic screening"));$("profile").firstChild.value="";
 for(const p of profiles){const option=element("option",`${p.body.name} / v${p.version}`);option.value=p.id;$("profile").append(option);}
 if(profiles.some(p=>p.id===chosen))$("profile").value=chosen;
}
async function refreshLogs(){
 const logs=await api("/api/logs");$("logs").replaceChildren();
 for(const log of logs){const button=element("button",`${log.filename}\n${log.status} · ${log.progress}%`,"log"+(selected===log.id?" active":""));button.type="button";button.addEventListener("click",run(()=>loadLog(log.id)));$("logs").append(button);}
 if(!logs.length)$("logs").append(element("p","No recordings yet.","muted"));
 if(selected&&job&&!["complete","partial","failed"].includes(job.status))await loadLog(selected);
}
async function loadLog(id){
 selected=id;const result=await api(`/api/logs/${id}`);if(selected!==id)return;job=result;
 $("welcome").hidden=true;$("job-header").hidden=false;$("filename").textContent=result.filename;
 $("progress").value=result.progress;$("job-status").textContent=`${result.status} · ${(result.size/1024/1024).toFixed(2)} MiB`+(result.error?` — ${result.error}`:"");
 $("analysis").hidden=!result.manifest;if(!result.manifest)return;
 const m=result.manifest;$("metrics").replaceChildren();
 for(const [name,value] of [["Records",m.record_count],["Sources",m.sources.length],["Events",m.event_count],["Findings",m.findings.length]]){
   const card=element("div",name,"metric");card.append(element("strong",value.toLocaleString()));$("metrics").append(card);
 }
 $("quality").textContent=result.status==="partial"?"Partial/salvaged dataset: some records or time relationships are uncertain. See diagnostics below.":"Decoded dataset. Coverage remains limited to recorded signals; no findings does not mean healthy.";
 $("profile-info").textContent=result.profile?`${result.profile.body.name}, profile v${result.profile.version} (operator-declared)`:"No attached profile";
 $("report-link").href=base()+"/report";$("original-link").href=base()+"/original";
 $("source").replaceChildren();
 for(const s of m.sources){const option=element("option",`${s.key} — ${s.role} (${s.messages.toLocaleString()})`);option.value=s.key;$("source").append(option);}
 const vehicle=m.sources.find(s=>s.role==="vehicle")||m.sources[0];$("source").value=vehicle.key;
 renderFindings(m.findings);renderMissions(m.missions);renderPhases(m);renderHypotheses(m.incidents||[]);
 const others=(await api("/api/logs")).filter(l=>l.id!==id&&["complete","partial"].includes(l.status));
 for(const select of [$("diff-against"),$("compare-with")])select.replaceChildren();
 const profileOption=element("option","Attached profile (expected parameters)");profileOption.value="";$("diff-against").append(profileOption);
 for(const l of others){for(const select of [$("diff-against"),$("compare-with")]){const o=element("option",`${l.filename} · ${l.created_at.slice(0,16)}`);o.value=l.id;select.append(o);}}
 if(result.profile)$("trend-key").value=result.profile.drone_key;
 $("diff").replaceChildren();$("diff-summary").textContent="";$("comparison").replaceChildren();
 await loadRules();
 $("limitations").replaceChildren(...m.limitations.map(x=>element("li",x)));
 $("diagnostics").replaceChildren();
 for(const d of m.diagnostics.slice(0,100))$("diagnostics").append(element("p",`${d.kind} at locator ${d.offset}: ${d.detail}`,"muted"));
 if(m.diagnostics.length>100)$("diagnostics").append(element("p","Showing the first 100 detailed diagnostics.","muted"));
 await sourceChanged();
}
async function sourceChanged(){
 const source=job.manifest.sources.find(s=>s.key===$("source").value);$("epoch").replaceChildren();
 for(const epoch of source.epoch_ids){const option=element("option",epoch);option.value=epoch;$("epoch").append(option);}
 $("signal").replaceChildren();
 for(const s of job.manifest.signals.filter(s=>s.source===source.key)){const option=element("option",`${s.name} [${s.unit}] · ${s.count} samples`);option.value=s.name;$("signal").append(option);}
 cursor=null;eventOffset=0;await Promise.all([loadPlots(),loadEvents(),loadParameters()]);
}
async function loadPlots(){
 const revision=++chartRevision,id=selected;const params=filters();
 const series=$("signal").value?api(`${base()}/series?${params}&signal=${encodeURIComponent($("signal").value)}`):Promise.resolve({points:[]});
 const [s,p]=await Promise.all([series,api(`${base()}/positions?${params}`)]);
 if(id!==selected||revision!==chartRevision)return;
 signalPoints=s.points;positionPoints=p.points;$("signal-title").textContent=$("signal").value||"No supported signal";
 drawPlots();
}
function evidenceButtons(ids){
 const box=element("div",undefined,"evidence-buttons");
 for(const id of [...new Set(ids)].slice(0,12)){const button=element("button",`#${id}`);button.addEventListener("click",run(()=>showEvidence(id)));box.append(button);}
 if(ids.length>12)box.append(element("span",`${ids.length} evidence references in total`,"muted"));
 return box;
}
function renderFindings(findings){
 $("findings").replaceChildren();
 if(!findings.length)$("findings").append(element("p","No supported rule triggered. Many checks may be unavailable; this is not a healthy-flight verdict.","notice"));
 for(const f of findings){
   const card=element("article",undefined,"finding "+f.severity);card.append(element("span",`${f.severity} · ${f.kind} · ${f.source}`,"badge"),element("h3",f.title),element("p",f.detail));
   if(f.missing.length)card.append(element("p","Needed to strengthen this: "+f.missing.join("; "),"muted"));
   card.append(evidenceButtons(f.evidence));$("findings").append(card);
 }
}
async function loadEvents(){
 if(!job?.manifest)return;
 const id=selected,revision=++eventRevision,params=filters();params.set("offset",eventOffset);params.set("limit",pageSize);params.set("collapse",$("collapse").checked);
 if($("search").value)params.set("search",$("search").value);if($("severity").value)params.set("severity",$("severity").value);
 const data=await api(`${base()}/events?${params}`);if(id!==selected||revision!==eventRevision)return;
 eventTotal=data.total;$("events").replaceChildren();
 for(const e of data.items){
   const tr=element("tr",undefined,e.severity);const timeCell=element("td");
   const jump=element("button",timeLabel(e.t_us));jump.addEventListener("click",run(()=>showEvidence(e.record_id)));timeCell.append(jump);
   const typeCell=element("td",e.kind);typeCell.append(element("div",e.severity,"badge"));
   const text=element("td",e.text);
   if(e.count>1)text.append(element("p",`×${e.count} across ${((e.last_us-e.t_us)/1e6).toFixed(1)} s — occurrences, not proven continuous state`,"muted"));
   text.append(evidenceButtons(e.evidence));tr.append(timeCell,typeCell,text);$("events").append(tr);
 }
 $("event-count").textContent=`${data.total} matching ${$("collapse").checked?"episodes":"events"}; showing ${data.items.length?eventOffset+1:0}–${eventOffset+data.items.length}`;
 $("previous-events").disabled=eventOffset===0;$("next-events").disabled=eventOffset+pageSize>=eventTotal;
}
async function loadParameters(){
 const id=selected,revision=++parameterRevision;const data=await api(`${base()}/parameters?source=${encodeURIComponent($("source").value)}&search=${encodeURIComponent($("parameter-search").value)}`);if(id!==selected||revision!==parameterRevision)return;
 const snapshots=data.filter(p=>p.epoch===Number($("epoch").value));$("parameters").replaceChildren();
 $("parameter-summary").textContent=snapshots.map(p=>`${Object.keys(p.values).length} matching names; expected total ${p.expected_count??"unknown"}; ${p.completeness}`).join(" | ")||"No observed parameter set for this source/epoch.";
 for(const snapshot of snapshots)for(const [name,value] of Object.entries(snapshot.values).sort().slice(0,200)){
   const tr=element("tr");tr.append(element("td",name),element("td",value.value===null?`raw: ${value.raw_value} (unverified)`:value.value),element("td",value.encoding));
   const td=element("td");td.append(evidenceButtons([value.record_id]));tr.append(td);$("parameters").append(tr);
 }
}
function renderMissions(missions){
 $("mission-limitation").textContent=missions.limitation;$("missions").replaceChildren();
 for(const m of missions.transactions){
   const node=element("details");node.append(element("summary",`${m.kind||m.direction}: ${m.count??"?"} items — ${m.status.replaceAll("_"," ")}`));
   for(const n of m.notes||[])node.append(element("p",n,"muted"));
   node.append(element("p",`${m.sender} → ${m.target}; clock epoch ${m.epoch}`,"muted"));
   for(const [seq,item] of Object.entries(m.items)){
     const command={16:"NAV_WAYPOINT",22:"NAV_TAKEOFF",20:"RETURN_TO_LAUNCH",21:"LAND"}[item.command]||`command ${item.command}`;
     const line=element("p",`Sequence ${seq}: ${command}, frame ${item.frame}, altitude field ${item.z}`);line.append(evidenceButtons([item.record_id]));node.append(line);
   }node.append(evidenceButtons(m.evidence));$("missions").append(node);
 }
 if(!missions.transactions.length)$("missions").append(element("p","No reconstructable full mission-transfer transaction observed.","muted"));
 renderExecution(missions);
}
function rel(t){return t==null||!job?.manifest?"—":`${((t-job.manifest.first_time_us)/1e6).toFixed(1)} s`;}
function renderPhases(m){
 $("phases").replaceChildren();
 if(!(m.phases||[]).length){$("phases").append(element("p","No armed episode observed.","muted"));return;}
 m.phases.forEach((p,i)=>{
   const box=element("div",undefined,"phase");
   box.append(element("strong",`Episode ${i} · ${p.source} · ${p.status.replaceAll("_"," ")}`));
   box.append(element("p",`Armed ${rel(p.armed_us)} · takeoff ${rel(p.takeoff_us)} · pre-event window ends ${rel(p.pre_event_end_us)} · attitude excursion ${rel(p.loss_of_control_onset_us)} · crash report ${rel(p.critical_event_us)} · disarmed ${rel(p.disarmed_us)} (relative to first record)`,"muted"));
   if(p.method)box.append(element("p","Method: "+p.method,"muted"));
   for(const note of p.notes)box.append(element("p",note,"muted"));
   box.append(evidenceButtons(p.evidence));$("phases").append(box);
 });
}
function renderHypotheses(incidents){
 $("hypotheses").replaceChildren();
 if(!incidents.length){$("hypotheses").append(element("p","No crash-check incident observed; hypotheses are only ranked for incident episodes.","muted"));return;}
 for(const incident of incidents){
   $("hypotheses").append(element("h3",`Episode ${incident.episode} · ${incident.source} · crash report at ${rel(incident.critical_event_us)}`));
   incident.hypotheses.forEach((h,i)=>{
     const card=element("div",undefined,"hypothesis");
     card.append(element("span",`#${i+1} · ${h.label} · screening score ${h.score}`,"badge"),element("h3",h.title));
     const list=element("ul");
     for(const s of h.support)list.append(element("li",`Supports (${s.family}): ${s.title}`));
     for(const c of h.contradictions)list.append(element("li",`Contradicts: ${c.title} — ${c.reason}`));
     for(const b of h.blocked_by)list.append(element("li",`Blocked: ${b}`));
     if(!list.children.length)list.append(element("li","No assessed evidence for or against.","muted"));
     card.append(list,element("p","Would discriminate: "+h.discriminating_evidence,"muted"));$("hypotheses").append(card);
   });
 }
}
async function loadRules(){
 const id=selected,params=new URLSearchParams();if($("rule-state").value)params.set("state",$("rule-state").value);
 const data=await api(`${base()}/rules?${params}`);if(id!==selected)return;
 $("rule-pack").textContent=data.rule_pack?`Rule pack ${data.rule_pack.pack} v${data.rule_pack.version} · ${data.rule_pack.rules} rules · sha256 ${data.rule_pack.sha256.slice(0,16)}`:"";
 $("rules").replaceChildren();
 for(const r of data.items){
   const tr=element("tr"),scope=r.scope;
   const label=scope.type==="episode"?`episode ${scope.episode}`:scope.type==="series"?scope.signal:scope.type==="event"?`event @ ${rel(scope.t_us)}`:`${scope.type} ${scope.source||""}`;
   const detail=element("td",r.detail);if(r.evidence.length)detail.append(evidenceButtons(r.evidence));
   const stateCell=element("td");stateCell.append(element("span",r.state.replace("_"," "),"state "+r.state));
   tr.append(element("td",`${r.title} (${r.rule_id} v${r.rule_version})`),element("td",label),stateCell,detail);
   $("rules").append(tr);
 }
}
async function runDiff(){
 const against=$("diff-against").value,params=new URLSearchParams();if(against)params.set("against_log",against);
 const d=await api(`${base()}/parameter-diff?${params}`);$("diff").replaceChildren();
 const oneSide=(d.only_left||[]).length+(d.only_right||[]).length;
 $("diff-summary").textContent=`${d.changed.length} changed · ${(d.unverified_changes||[]).length} raw-only changes (encoding unverified) · ${oneSide} names seen on one side only${d.not_observed?.length?` · ${d.not_observed.length} expected names not observed`:""}. ${d.notes.join(" ")}`;
 for(const c of d.changed){
   const tr=element("tr"),left=element("td",c.left);if(c.left_record)left.append(evidenceButtons([c.left_record]));
   tr.append(element("td",c.name),left,element("td",c.right),element("td",c.meta?.description||""));$("diff").append(tr);
 }
 if(!d.changed.length){const tr=element("tr"),td=element("td","No typed differences.","muted");td.colSpan=4;tr.append(td);$("diff").append(tr);}
}
function metricTable(rows,headers){
 const wrap=element("div",undefined,"table-wrap"),table=element("table"),head=element("tr"),thead=element("thead"),body=element("tbody");
 for(const h of headers)head.append(element("th",h));thead.append(head);
 for(const row of rows){const tr=element("tr");for(const cell of row)tr.append(element("td",cell==null?"—":typeof cell==="number"?Number(cell.toFixed(3)):cell));body.append(tr);}
 table.append(thead,body);wrap.append(table);return wrap;
}
async function runCompare(){
 if(!$("compare-with").value)throw new Error("Choose another analysed log");
 const c=await api(`/api/compare?left=${selected}&right=${$("compare-with").value}`);$("comparison").replaceChildren();
 $("comparison").append(element("h3",`${c.left.filename} (episode ${c.left.flight?.episode??"—"}) vs ${c.right.filename} (episode ${c.right.flight?.episode??"—"})`));
 $("comparison").append(metricTable(c.metrics.map(m=>[m.metric,m.left,m.right,m.delta]),["Metric (pre-event airborne)","This log","Other","Δ"]));
 $("comparison").append(metricTable(c.rules.map(r=>[r.rule_id,r.left,r.right]),["Rule","This log","Other"]));
 if(!c.parameters.error)$("comparison").append(element("p",`Parameters: ${c.parameters.changed.length} typed differences ${c.parameters.changed.slice(0,12).map(x=>`${x.name} ${x.left}→${x.right}`).join(", ")}`,"muted"));
 for(const n of c.notes)$("comparison").append(element("p",n,"muted"));
}
async function runTrends(){
 const key=$("trend-key").value.trim();if(!key)throw new Error("Enter an aircraft key");
 const t=await api(`/api/drones/${encodeURIComponent(key)}/trends`);$("comparison").replaceChildren();
 $("comparison").append(element("h3",`${t.flights.length} airborne flight(s) for ${t.drone_key}`));
 const keys=["vibration_z_mean","vibration_z_max","ekf_compass_ratio_max","tilt_error_deg_mean","throttle_pct_mean","motor_spread"];
 $("comparison").append(metricTable(t.flights.map(f=>[f.filename,f.episode,f.phase_status.replace("airborne_",""),...keys.map(k=>f[k]),f.crash_check?"yes":"no"]),["Log","Ep.","Takeoff",...keys,"Crash report"]));
 for(const n of t.notes)$("comparison").append(element("p",n,"muted"));
}
function renderExecution(missions){
 $("mission-execution").replaceChildren();
 for(const v of missions.plan_versions||[])$("mission-execution").append(element("p",`Plan v${v.id}: ${v.item_count} items · ${v.confidence.replaceAll("_"," ")} · ${v.basis} · at ${rel(v.t_us)}`,"muted"));
 for(const e of missions.execution||[]){
   const node=element("details");node.append(element("summary",`Episode ${e.episode}: plan v${e.plan_version??"unknown"} ${e.plan_confidence?`(${e.plan_confidence.replaceAll("_"," ")})`:""}`));
   for(const n of e.notes)node.append(element("p",n,"muted"));
   for(const item of e.items){const line=element("p",`Item ${item.seq} ${item.command}: ${item.status.replaceAll("_"," ")}. ${item.note||""}`);line.append(evidenceButtons(item.evidence));node.append(line);}
   $("mission-execution").append(node);
 }
}
async function showEvidence(id){
 const logId=selected;const record=await api(`${base()}/records/${id}`);if(logId!==selected)return;
 if($("source").value!==record.source){$("source").value=record.source;await sourceChanged();}
 if($("epoch").value!==String(record.epoch)){$("epoch").value=record.epoch;await Promise.all([loadPlots(),loadEvents(),loadParameters()]);}
 cursor={t:record.t_us,id:record.id,epoch:record.epoch};drawPlots();
 $("cursor").textContent=`Selected #${id} · ${timeLabel(record.t_us)} · ${record.clock} · epoch ${record.epoch}`;
 $("evidence-label").textContent=`${record.message} from ${record.source}; ${record.locator_kind} ${record.offset}; ${record.quality}`;
 $("evidence").textContent=JSON.stringify(record,null,2);$("evidence-panel").hidden=false;
}
function drawPlot(canvas,points,isMap){
 const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);ctx.fillStyle="#aec0d6";ctx.font="16px system-ui";
 if(!points.length){ctx.fillText("No supported observations for this selection",25,50);return;}
 const xs=points.map(p=>isMap?p.lon:p.t_us),ys=points.map(p=>isMap?p.lat:p.value);
 let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
 for(let i=0;i<xs.length;i++){minX=Math.min(minX,xs[i]);maxX=Math.max(maxX,xs[i]);minY=Math.min(minY,ys[i]);maxY=Math.max(maxY,ys[i]);}
 const x=v=>50+(w-75)*(v-minX)/(maxX-minX||1),y=v=>h-40-(h-70)*(v-minY)/(maxY-minY||1);
 ctx.strokeStyle="#344861";ctx.strokeRect(50,30,w-75,h-70);ctx.fillText(maxY.toFixed(3),4,25);ctx.fillText(minY.toFixed(3),4,h-15);
 ctx.fillText(isMap?"longitude / latitude":timeLabel(minX),65,h-8);
 ctx.strokeStyle="#70dfd0";ctx.lineWidth=2;ctx.beginPath();
 for(let i=0;i<points.length;i++){
   const p=points[i],previous=points[i-1];
   if(!previous||p.epoch!==previous.epoch||p.t_us-previous.t_us>5e6)ctx.moveTo(x(xs[i]),y(ys[i]));else ctx.lineTo(x(xs[i]),y(ys[i]));
 }ctx.stroke();
 if(cursor){
   const eligible=points.filter(p=>p.epoch===cursor.epoch);
   const p=eligible.reduce((best,p)=>!best||Math.abs(p.t_us-cursor.t)<Math.abs(best.t_us-cursor.t)?p:best,null);
   if(p&&Math.abs(p.t_us-cursor.t)<=5e6){ctx.fillStyle="#f8b96c";ctx.beginPath();ctx.arc(x(isMap?p.lon:p.t_us),y(isMap?p.lat:p.value),6,0,Math.PI*2);ctx.fill();}
 }
 canvas.onclick=run(async event=>{
   const rect=canvas.getBoundingClientRect(),cx=(event.clientX-rect.left)*w/rect.width,cy=(event.clientY-rect.top)*h/rect.height;
   let best=null,dist=Infinity;
   for(let i=0;i<points.length;i++){const d=(x(xs[i])-cx)**2+(y(ys[i])-cy)**2;if(d<dist){dist=d;best=points[i];}}
   if(best)await showEvidence(best.id);
 });
}
function drawPlots(){drawPlot($("chart"),signalPoints,false);drawPlot($("map"),positionPoints,true);}
$("upload-form").addEventListener("submit",run(async event=>{
 event.preventDefault();const file=$("file").files[0];if(!file)return;
 if(file.size>100*1024*1024)throw new Error("Local upload limit: 100 MiB");
 const form=new FormData();form.append("file",file);if($("profile").value)form.append("profile_id",$("profile").value);
 $("upload-button").disabled=true;$("upload-button").textContent="Uploading…";
 try{const response=await api("/api/logs",{method:"POST",body:form});await loadLog(response.id);await refreshLogs();}
 finally{$("upload-button").disabled=false;$("upload-button").textContent="Upload and analyse";}
}));
$("profile-form").addEventListener("submit",run(async event=>{
 event.preventDefault();const p=await api("/api/profiles",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
   drone_key:$("drone-key").value,name:$("drone-name").value,frame:$("frame").value,firmware:$("firmware").value,
   notes:$("notes").value,vibration_warning:Number($("vibration").value),expected_parameters:parseExpected($("expected-params").value)
 })});await refreshProfiles();$("profile").value=p.id;
}));
function parseExpected(text){
 const result={};
 for(const line of text.split(/\r?\n/)){const t=line.trim();if(!t)continue;const m=t.match(/^([A-Z0-9_]{1,16})\s*=\s*(-?[0-9.eE+-]+)$/);if(!m)throw new Error(`Expected NAME=value, got: ${t}`);result[m[1]]=Number(m[2]);}
 return result;
}
$("source").addEventListener("change",run(sourceChanged));
$("rule-state").addEventListener("change",run(loadRules));
$("run-diff").addEventListener("click",run(runDiff));
$("run-compare").addEventListener("click",run(runCompare));
$("run-trends").addEventListener("click",run(runTrends));
$("epoch").addEventListener("change",run(async()=>{cursor=null;eventOffset=0;await Promise.all([loadPlots(),loadEvents(),loadParameters()]);}));
$("signal").addEventListener("change",run(loadPlots));
$("filter-events").addEventListener("click",run(async()=>{eventOffset=0;await loadEvents();}));
$("previous-events").addEventListener("click",run(async()=>{eventOffset=Math.max(0,eventOffset-pageSize);await loadEvents();}));
$("next-events").addEventListener("click",run(async()=>{eventOffset+=pageSize;await loadEvents();}));
$("filter-parameters").addEventListener("click",run(loadParameters));
$("close-evidence").addEventListener("click",()=>$("evidence-panel").hidden=true);
async function tick(){const health=await api("/api/health");$("health").textContent=health.worker_active?"Worker ready · local only":"Worker not active";await refreshLogs();}
run(async()=>{await refreshProfiles();await tick();const id=new URLSearchParams(location.search).get("log");if(id)await loadLog(id);})();
setInterval(run(tick),3000);
