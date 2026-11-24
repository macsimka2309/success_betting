// Functions execute against sports containers only; no account DOM is serialized.
function eventRows(root, withAudit=false) {
  const list = root.matches?.('[class*="virtual-list--"]') ? root : root.querySelector('[class*="virtual-list--"]');
  if (!list) throw new Error('live_list_unavailable');
  const headers=[...list.querySelectorAll('[class*="sport-competition--"]')].map(e=>({
    top:parseFloat(e.closest('[style*="top:"]')?.style.top)||0,
    name:[...e.children].find(c=>c.className?.includes('table-component-text--'))?.textContent.trim()
  })).sort((a,b)=>a.top-b.top);
  const rows=[];
  const events=[...list.querySelectorAll('[class*="sport-base-event--"]')].flatMap(row=>{
    const link=row.querySelector('a[href^="/live/football/"]') || row.querySelector('a[href]');
    // Verified compact single-selection rows reuse the event class. A generic
    // missing link or any match-like structure must still fail the coverage audit.
    const singleNames=[...row.querySelectorAll('[class*="sport-event__name--"]')];
    const singleName=singleNames.length===1?singleNames[0].textContent.trim():'';
    const classTokens=[...row.classList];
    const excludedNonMatch=!link && classTokens.some(c=>c.startsWith('_compact--')) &&
      classTokens.some(c=>c.startsWith('_without-other-factors-count--')) &&
      singleName.length>0 && !/\s+[—–-]\s+/.test(singleName) &&
      !row.querySelector('[class*="event-block-score--"], [class*="event-block-current-time"], [class*="period--"], [resource-name="translation_mc_soccer"], [class*="sport-base-event--"]');
    const match=link?.getAttribute('href').match(/^\/live\/football\/(?:(?:country\/[A-Za-z0-9-]+|category\/(?:internationalteams|internationalclubs))\/)?(\d+)\/(\d+)\/?$/);
    const excludedRoute=link?.getAttribute('href').match(/^\/live\/football\/category\/cyberfootball\/(\d+)\/(\d+)\/?$/);
    const names=link?.textContent.trim().split(/\s+—\s+/);
    const href=link?.getAttribute('href');
    let path='<missing>';
    if(href){try{const url=new URL(href,'https://fon.bet');path=url.origin==='https://fon.bet'?url.pathname:'<external>';}catch{path='<invalid>';}}
    const issue=excludedRoute||excludedNonMatch?null:!link?'missing_link':!match?'unsupported_event_url':names?.length!==2?'invalid_team_names':null;
    rows.push({key:(match||excludedRoute)?'event:'+(match||excludedRoute)[2]:'row:'+path+':'+(link?.textContent||row.textContent).trim().slice(0,100),
      path,format:excludedNonMatch?'compact_single_selection':excludedRoute?'category_cyberfootball':match?(href.includes('/country/')?'country':href.includes('/category/internationalteams/')?'category_internationalteams':href.includes('/category/internationalclubs/')?'category_internationalclubs':'short'):'unknown',
      excluded_route:Boolean(excludedRoute),excluded_non_match:excludedNonMatch,issue});
    if(excludedNonMatch || excludedRoute || !match || names?.length!==2)return [];
    const top=parseFloat(row.closest('[style*="top:"]')?.style.top)||0;
    const score=row.querySelector('[class*="event-block-score--"]')?.textContent.trim().match(/^(\d+):(\d+)/);
    return [{fonbet_event_id:match[2],event_url:'https://fon.bet'+link.getAttribute('href'),
      home_name:names[0],away_name:names[1],competition_name:headers.filter(h=>h.top<=top).at(-1)?.name || '',
      clock_text:row.querySelector('[class*="event-block-current-time__time--"]')?.textContent.trim() || '',
      format_tags:[...[...row.querySelectorAll('*')].filter(e=>!e.children.length).map(e=>e.textContent.trim()).join(' ').matchAll(/(?:^|[^\d])(2\s*[xх×]\s*40)(?!\d)/gi)].map(m=>m[1]),
      period_text:row.querySelector('[class*="period--"]')?.textContent.trim() || '',
      goals_home:score?Number(score[1]):null,goals_away:score?Number(score[2]):null}];
  });
  return withAudit ? {events,rows} : events;
}
function scrollList(root, reset) {
  let scroller=root.parentElement;
  while(scroller && !(scroller.scrollHeight>scroller.clientHeight+2 && /auto|scroll/.test(getComputedStyle(scroller).overflowY)))scroller=scroller.parentElement;
  scroller=scroller || document.scrollingElement;
  if(reset)scroller.scrollTop=0;
  const before={top:scroller.scrollTop,height:scroller.clientHeight,total:scroller.scrollHeight};
  if(!reset)scroller.scrollTop+=scroller.clientHeight*.7;
  return before;
}
function selectedPeriod(root) {
  const labels=[...root.querySelectorAll('[aria-selected="true"], [aria-pressed="true"], [class*="_selected--"], [class*="_active--"], [class*="tabs-item__select--"]')];
  const text=labels.map(e=>e.textContent.trim()).filter(t=>/^(2\s*тайм|2-й тайм|Второй тайм)$/i.test(t));
  return text.length===1 ? text[0] : null;
}
function statistic(root) {
  const teams=[...root.querySelectorAll('[class*="rowTeam--"] [class*="colName--"]')].map(e=>e.textContent.trim());
  const score=[...root.querySelectorAll('[class*="rowTeam--"] [class*="_large--"]')].map(e=>e.textContent.trim());
  // Identify the period block by its headers, never by an unlabelled column index.
  const scoreboard=root.querySelector('[class*="score-block--"]');
  const headers=scoreboard ? [...scoreboard.querySelectorAll('[class*="rowHeader--"] [class*="colBlock--"]')] : [];
  const periodBlocks=headers.map((block,index)=>({index,labels:[...block.querySelectorAll('[class*="head--"]')].map(e=>e.textContent.trim())}))
    .filter(block=>block.labels.length===2 && block.labels[0]==='1' && block.labels[1]==='2');
  const teamRows=scoreboard ? [...scoreboard.querySelectorAll('[class*="rowTeam--"]')] : [];
  let halves=[null,null];
  if(periodBlocks.length===1 && teamRows.length===2){
    halves=teamRows.map((row,side)=>{
      const block=[...row.querySelectorAll('[class*="colBlock--"]')][periodBlocks[0].index];
      const values=block ? [...block.querySelectorAll('[class*="cell--"]')].map(e=>e.textContent.trim()) : [];
      return values.length===2 && values.every(v=>/^\d{1,4}$/.test(v)) && /^\d{1,4}$/.test(score[side]) &&
        Number(values[0])+Number(values[1])===Number(score[side]) ? Number(values[0]) : null;
    });
  }
  const rows=[...root.querySelectorAll('[class*="value-row--"]')].filter(e=>{
    const label=e.querySelector('[class*="caption--"]')?.textContent.trim();
    return label==='Ударов в створ'||label==='Удары в створ';
  });
  if(teams.length!==2)throw new Error('invalid_teams');
  if(rows.length!==1)throw new Error('metric_missing_or_ambiguous');
  const row=rows[0];
  return {home_name:teams[0],away_name:teams[1],ht_home:halves[0],ht_away:halves[1],goals_home:/^\d+$/.test(score[0])?Number(score[0]):null,
    goals_away:/^\d+$/.test(score[1])?Number(score[1]):null,
    clock_text:root.querySelector('[class*="scoreboard-timer__value--"]')?.textContent.trim() || '',
    source_metric_label:row.querySelector('[class*="caption--"]').textContent.trim(),
    home_value:row.querySelector('[class*="_v1--"]')?.textContent.trim() ?? null,
    away_value:row.querySelector('[class*="_v2--"]')?.textContent.trim() ?? null};
}
module.exports={eventRows,scrollList,selectedPeriod,statistic};
