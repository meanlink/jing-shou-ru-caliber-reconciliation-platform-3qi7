import {weekly, datasets} from '../api/entities.js';
import {weeklyDownload} from '../api/client.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, fmt, date, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const STATUS_TEXT = {queued: '待评估', held: '并发挂起·待重审', blocked: '已拦截', published: '已发布'};
const REASON_TEXT = {
  primary_low_visits: '访问不足100次，净收入按规则主遮挡',
  secondary_inference_risk: '同一受众可由已发布周报（含差值拼合）推断，追加遮挡',
  secondary_protect_total: '为避免经合计行反推，连带遮挡',
  concurrent_review_pending: '与另一份周报并发分发且合并存在泄露，待先发完成后重审',
  empty_release_blocked: '全部行均被遮挡，无可用发布内容',
};

function reasonBadge(codes) {
  return (codes || []).map(c => '<span class="mask-reason" title="' + esc(REASON_TEXT[c] || c) + '">' + esc(REASON_TEXT[c] || c) + '</span>').join('');
}

function decisionTable(decision) {
  if (!decision || !decision.rows) return '<p class="empty">暂无评估结果。</p>';
  return '<div class="table-wrap"><table><thead><tr><th>对账行</th><th>访问量</th><th>日净收入(CNY)</th><th>状态</th><th>拦截/遮挡原因</th></tr></thead><tbody>' +
    decision.rows.map(r => '<tr class="' + (r.suppressed ? 'row-masked' : '') + '"><td>' + esc(r.label) + '</td><td>' + fmt(r.visits) + '</td><td>' +
      (r.suppressed ? '<span class="masked-value">***</span>' : esc(fmt(r.net_income))) + '</td><td>' +
      (r.suppressed ? '已遮挡' : '可见') + '</td><td>' + reasonBadge(r.reasons) + '</td></tr>').join('') +
    '</tbody></table></div><p class="hint">试看仅为遮挡效果预览，不产生披露记录；实际下载以已发布内容为准，被遮金额在文件中显示为 ***。</p>';
}

export async function weeklyPage() {
  const [list, deliveries, batches] = await Promise.all([weekly.list(), weekly.deliveries(), datasets.list()]);
  const isAdmin = session.user.role === 'admin';
  return heading('06 / WEEKLY MASKING', '经营周报脱敏与分发', '访问不足百次的渠道日净收入强制遮挡；同受众多份周报按线性推断（含差值拼合）二次拦截；并发分发串行化，恢复重试不新增披露。',
      '<button class="button" id="refresh-weekly">刷新 ↻</button>') +
    '<div class="report-grid"><section class="panel"><div class="section-title"><h2>试看遮挡效果</h2><span>不落库·不披露</span></div>' +
    '<form id="weekly-preview-form">' +
    '<label>源账批次<select name="dataset_id" required>' + batches.map(b => '<option value="' + b.id + '">' + esc(b.name) + '</option>').join('') + '</select></label>' +
    '<label>接收受众<input name="audience" value="经营周会 · 本地收件箱" required></label>' +
    '<label>分组方式<select name="group_by"><option value="channel_day">渠道日（规则原文口径）</option><option value="channel">按渠道汇总</option><option value="week">按自然周</option><option value="channel_week">渠道×自然周</option></select></label>' +
    '<label>渠道筛选（留空=全部）<input name="channels" placeholder="多个渠道用逗号分隔，如：直营内容,合作渠道"></label>' +
    '<div class="parameter-grid"><label>开始日期<input type="date" name="date_from"></label><label>结束日期<input type="date" name="date_to"></label></div>' +
    '<button type="submit" class="primary">试看遮挡与拦截原因</button></form>' +
    '<div id="weekly-preview-result" class="preview-box"><p class="empty">选择筛选后试看，被遮金额显示为 ***。</p></div></section>' +

    '<section><div class="section-title"><h2>周报分发计划</h2><span>' + list.length + ' 份</span></div>' +
    '<form id="weekly-form" class="panel compact-form"><label>简报名称<input name="name" required value="经营周报 · 渠道日净收入"></label>' +
    '<label>源账批次<select name="dataset_id" required>' + batches.map(b => '<option value="' + b.id + '">' + esc(b.name) + '</option>').join('') + '</select></label>' +
    '<label>接收受众<input name="audience" value="经营周会 · 本地收件箱" required></label>' +
    '<label>分组方式<select name="group_by"><option value="channel_day">渠道日</option><option value="channel">按渠道</option><option value="week">按自然周</option><option value="channel_week">渠道×自然周</option></select></label>' +
    '<label>渠道筛选（留空=全部）<input name="channels" placeholder="直营内容,合作渠道"></label>' +
    '<div class="parameter-grid"><label>分发周期<select name="interval_seconds"><option value="604800">每周</option><option value="86400">每日</option><option value="60">每分钟（验收）</option></select></label><label>首次执行<select name="delay_seconds"><option value="0">立即</option><option value="60">1 分钟后</option></select></label></div>' +
    '<button type="submit" class="primary" ' + (isAdmin ? '' : 'disabled') + '>建立周报计划</button>' +
    '<p class="hint">同批源账可换筛选建立多份；系统按受众维护披露台账，自动拦截差值推断。</p></form>' +

    '<div class="schedule-list">' + list.map(r => '<article><div><strong>' + esc(r.name) + '</strong><p>' + esc(r.audience) + ' · ' + esc(r.group_by) + '</p><small>下次 ' + date(r.next_run) + ' · 状态 <em class="st-' + esc(r.status) + '">' + esc(STATUS_TEXT[r.status] || r.status) + '</em></small></div>' +
      '<div class="row-actions"><button class="button small" data-weekly-run="' + r.id + '" ' + (isAdmin ? '' : 'disabled') + '>立即评估</button>' +
      '<button class="button small" data-weekly-toggle="' + r.id + '" ' + (isAdmin ? '' : 'disabled') + '>' + (r.enabled ? '暂停' : '启用') + '</button></div></article>').join('') + '</div>' +

    '<section class="panel inbox"><div class="section-title"><h2>发布记录与拦截原因</h2><span>' + deliveries.length + ' 条</span></div>' +
    deliveries.map(d => '<div class="delivery"><div><strong>#' + d.id + ' · ' + esc(d.audience) + '</strong><p>' + date(d.created_at) + ' · 尝试 ' + d.attempts + ' 次 · <em class="st-' + esc(d.status) + '">' + esc(STATUS_TEXT[d.status] || d.status) + '</em></p>' +
      '<small class="decision-reason">' + esc(d.decision?.decision_reason || '') + '</small></div><div>' +
      '<button class="text-button" data-weekly-reasons="' + d.id + '">拦截原因</button>' +
      (d.status === 'held' ? '<button class="text-button" data-weekly-recheck="' + d.id + '" ' + (isAdmin ? '' : 'disabled') + '>重审</button>' : '') +
      (d.status === 'published' ? '<button class="text-button" data-weekly-download="' + d.id + '">CSV ↓</button>' : '<span class="no-download">无下载</span>') +
      '</div></div>').join('') + (!deliveries.length ? '<p class="empty">计划执行后，发布、挂起与拦截记录会出现在此处。</p>' : '') +
    '<div id="weekly-reasons-box"></div></section></section></div>';
}

function parseFilters(form) {
  const f = new FormData(form);
  const channels = (f.get('channels') || '').split(',').map(s => s.trim()).filter(Boolean);
  return {
    dataset_id: Number(f.get('dataset_id')), audience: f.get('audience'), group_by: f.get('group_by'),
    channels, date_from: f.get('date_from') || null, date_to: f.get('date_to') || null,
  };
}

export function bindWeekly(refresh) {
  document.querySelector('#refresh-weekly').onclick = refresh;

  submit(document.querySelector('#weekly-preview-form'), async form => {
    try {
      const decision = await weekly.preview(parseFilters(form));
      document.querySelector('#weekly-preview-result').innerHTML =
        '<div class="section-title"><h2>试看结果</h2><span class="st-' + esc(decision.status) + '">' + esc(STATUS_TEXT[decision.status] || decision.status) + '</span></div>' +
        decisionTable(decision);
    } catch (e) { notify(e.message, true); }
  });

  submit(document.querySelector('#weekly-form'), async form => {
    const f = new FormData(form);
    await weekly.create({
      name: f.get('name'), ...parseFilters(form),
      interval_seconds: Number(f.get('interval_seconds')), delay_seconds: Number(f.get('delay_seconds')),
    });
    notify('周报计划已保存，将按披露控制规则评估发布');
    await refresh();
  });

  document.querySelectorAll('[data-weekly-toggle]').forEach(b => b.onclick = async () => {
    try { await weekly.toggle(b.dataset.weeklyToggle); await refresh(); } catch (e) { notify(e.message, true); }
  });
  document.querySelectorAll('[data-weekly-run]').forEach(b => b.onclick = async () => {
    try { const d = await weekly.run(b.dataset.weeklyRun); notify('评估完成：' + (STATUS_TEXT[d.status] || d.status)); await refresh(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelectorAll('[data-weekly-recheck]').forEach(b => b.onclick = async () => {
    try { const d = await weekly.recheck(b.dataset.weeklyRecheck); notify('重审完成：' + (STATUS_TEXT[d.status] || d.status)); await refresh(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelectorAll('[data-weekly-download]').forEach(b => b.onclick = () =>
    weeklyDownload(b.dataset.weeklyDownload).catch(e => notify(e.message, true)));
  document.querySelectorAll('[data-weekly-reasons]').forEach(b => b.onclick = async () => {
    try {
      const r = await weekly.reasons(b.dataset.weeklyReasons);
      const rows = r.rows.map(x => '<tr><td>' + esc(x.row) + '</td><td>' + (x.suppressed ? '遮挡' : '可见') + '</td><td>' + reasonBadge(x.reasons) + '</td></tr>').join('');
      document.querySelector('#weekly-reasons-box').innerHTML =
        '<section class="panel"><div class="section-title"><h2>#' + r.id + ' 拦截原因明细</h2><span>' + esc(r.status) + '</span></div>' +
        '<p class="warning">' + esc(r.decision_reason || '') + '</p><div class="table-wrap"><table><thead><tr><th>对账行</th><th>结论</th><th>原因</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
    } catch (e) { notify(e.message, true); }
  });
}
