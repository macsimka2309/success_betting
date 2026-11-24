'use strict';
function clockMinute(text) {
  const match = String(text || '').trim().match(/^(\d{1,3})(?::([0-5]\d))?(?:[′'’])?$/);
  return match ? Number(match[1]) : null;
}
function excludedFormat(event) {
  const competition=String(event.competition_name || '').trim();
  const tags=Array.isArray(event.format_tags)?event.format_tags:[];
  const text=[competition,event.period_text || '',...tags].join(' ');
  if (/^FC/i.test(competition)) return 'cyberfootball';
  if (/футзал/i.test(competition)) return 'futsal';
  if (/(?:^|[^\d])2\s*[xх×]\s*40(?!\d)/i.test(text)) return 'format_2x40';
  if (/esports|h2h|кибер|мини.?футбол|\b[23][xх×][23]\b|[12][xх×](?!45\b)\d+\s*мин/i.test(competition)) return 'nonstandard_football';
  return null;
}
function standardFootball(competition) {
  return Boolean(String(competition || '').trim()) && !excludedFormat({competition_name:competition});
}
function candidate(event) {
  const minute = clockMinute(event.clock_text);
  return standardFootball(event.competition_name) && !excludedFormat(event) && minute >= 65 && minute <= 75 &&
    minute !== null && !/перерыв|окончен|заверш|пенальти|доп\.?\s*время|\bET\b/i.test(event.period_text || '') &&
    (!event.period_text || /^(?:2\s*тайм|2-й тайм|Второй тайм)$/i.test(event.period_text.trim()));
}
function count(text) {
  const value = String(text ?? '').trim();
  if (!value || /^[-–—]$/.test(value)) return null;
  if (!/^\d{1,4}$/.test(value)) throw new Error('invalid_statistic');
  return Number(value);
}
function normalize(event, data) {
  if (!/^\d+$/.test(event.fonbet_event_id) || !event.home_name || !event.away_name ||
      event.home_name === event.away_name) throw new Error('invalid_event');
  if (!candidate({...event, ...data})) throw new Error('outside_window');
  if (!/^(?:2\s*тайм|2-й тайм|Второй тайм)$/i.test(data.source_period_label || '') ||
      !['Удары в створ','Ударов в створ'].includes(data.source_metric_label)) throw new Error('invalid_period_or_metric');
  if (data.home_name !== event.home_name || data.away_name !== event.away_name) throw new Error('team_mismatch');
  if (!data.period_confirmed) throw new Error('unconfirmed_period');
  const observed = Date.parse(data.observed_at), context = Date.parse(data.context_observed_at);
  const requested = Date.parse(data.requested_at);
  if (![observed, context, requested].every(Number.isFinite) || observed < context ||
      observed-context > 30000 || observed < requested) throw new Error('context_stale');
  // Raw shots stay untouched; ht is context for SQL normalization, not an increment.
  for(const side of ['home','away']){
    const ht=data['ht_'+side], goals=data['goals_'+side];
    if(ht!=null && (!Number.isInteger(ht) || ht<0 || !Number.isInteger(goals) || ht>goals))
      throw new Error('invalid_half_score');
  }
  const home = count(data.home_value), away = count(data.away_value);
  if (home === null && away === null) throw new Error('empty');
  return {...event, ...data, period: '2H', elapsed: clockMinute(data.clock_text),
    shots_on_target_2h_home: home, shots_on_target_2h_away: away,
    shots_on_target_2h_total: home === null || away === null ? null : home+away,
    quality_status: home === null || away === null ? 'partial' : 'both_teams'};
}
module.exports = {clockMinute, standardFootball, excludedFormat, candidate, count, normalize};
