// Cgroup counters only: never browser profile, environment, account or credentials.
'use strict';
const fs=require('node:fs');
function snapshot(read=path=>fs.readFileSync(path,'utf8')){
  const number=path=>{try{const s=read(path).trim();return /^\d+$/.test(s)?Number(s):null;}catch{return null;}};
  let events={},generation=null;
  try{events=Object.fromEntries(read('/sys/fs/cgroup/memory.events').trim().split('\n').map(s=>{const [k,v]=s.split(/\s+/);return [k,Number(v)];}));}catch{}
  try{const s=read('/proc/1/stat');generation=s.slice(s.lastIndexOf(')')+2).split(/\s+/)[19];}catch{}
  let stat={};try{stat=Object.fromEntries(read('/sys/fs/cgroup/memory.stat').trim().split('\n').map(line=>{const [k,v]=line.split(/\s+/);return [k,Number(v)];}));}catch{}
  const current=number('/sys/fs/cgroup/memory.current');
  return {generation,current_bytes:current,working_set_bytes:current!==null&&Number.isFinite(stat.inactive_file)?Math.max(0,current-stat.inactive_file):null,
    anon_bytes:stat.anon??null,inactive_file_bytes:stat.inactive_file??null,limit_bytes:number('/sys/fs/cgroup/memory.max'),
    cgroup_peak_bytes:number('/sys/fs/cgroup/memory.peak'),oom_kill:events.oom_kill??null,oom:events.oom??null};
}
function monitorMemory(read=snapshot){
  const checkpoints=[];
  const before=read();let peak=before.current_bytes||0;
  const sample=()=>{const v=read();peak=Math.max(peak,v.current_bytes||0);return v;};
  const timer=setInterval(sample,500);timer.unref();
  return {mark(stage,event_id=null){if(checkpoints.length<64)checkpoints.push({stage,event_id,...sample()});},stop(){clearInterval(timer);const after=sample();return {before,after,sampled_peak_bytes:peak,checkpoints,
    oom_kill_delta:before.oom_kill===null||after.oom_kill===null?null:Math.max(0,after.oom_kill-before.oom_kill)};}};
}
if(require.main===module)console.log(JSON.stringify(snapshot()));
module.exports={snapshot,monitorMemory};
